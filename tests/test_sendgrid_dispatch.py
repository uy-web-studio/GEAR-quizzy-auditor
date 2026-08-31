"""Unit and integration tests for SendGrid Dispatcher with Exponential Backoff.

Verifies:
1. Exponential backoff retry strategy (1s, 2s, 4s, 8s, 16s intervals with max 5 retries).
2. Intelligent error classification (retryable 429, 5xx, network errors vs fail-fast 4xx).
3. Retry-After header support.
4. Outage and storm prevention simulation under heavy concurrent traffic.
5. ReporterAgent pipeline integration.
"""

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from python_http_client.exceptions import HTTPError

from daily_audit_pipeline.sendgrid_dispatch import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_BASE_DELAY,
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRYABLE_STATUS_CODES,
    RetryConfig,
    SendGridDispatcher,
    _build_html_report,
    calculate_backoff_delay,
    is_retryable_error,
    send_audit_report,
    send_no_quiz_alert,
)


class MockSendGridResponse:
  """Mock response from SendGrid API client."""

  def __init__(self, status_code: int = 202, body: bytes = b"", headers: Optional[dict] = None):
    self.status_code = status_code
    self.body = body
    self.headers = headers or {}


class MockSendGridClient:
  """Configurable mock SendGrid client for testing retries and outages."""

  def __init__(self, side_effects: List[Any]):
    self.side_effects = list(side_effects)
    self.call_count = 0
    self.calls: List[Any] = []

  def send(self, mail: Any):
    self.call_count += 1
    self.calls.append(mail)
    if not self.side_effects:
      return MockSendGridResponse(202)
    effect = self.side_effects.pop(0)
    if isinstance(effect, Exception):
      raise effect
    return effect


class TestCalculateBackoffDelay:
  """Tests for exponential backoff delay calculation."""

  def test_default_exponential_intervals(self):
    """Verify the 1s, 2s, 4s, 8s, 16s sequence for 5 retry attempts."""
    expected_delays = [1.0, 2.0, 4.0, 8.0, 16.0]
    actual_delays = [calculate_backoff_delay(attempt=i) for i in range(5)]
    assert actual_delays == expected_delays

  def test_negative_attempt_clamped_to_zero(self):
    """Negative attempt indices should be treated as 0 (1.0s base delay)."""
    assert calculate_backoff_delay(attempt=-1) == 1.0
    assert calculate_backoff_delay(attempt=-5) == 1.0

  def test_max_delay_cap(self):
    """Delays exceeding max_delay should be capped."""
    delay = calculate_backoff_delay(
        attempt=10, base_delay=1.0, backoff_factor=2.0, max_delay=32.0
    )
    assert delay == 32.0

  def test_retry_after_header_override(self):
    """Explicit Retry-After header should take precedence if greater than calculated backoff."""
    # Retry 0 calculated is 1.0s, retry_after is 10.0s -> delay should be 10.0s
    delay1 = calculate_backoff_delay(attempt=0, retry_after=10.0)
    assert delay1 == 10.0

    # Retry 4 calculated is 16.0s, retry_after is 5.0s -> delay should remain 16.0s
    delay2 = calculate_backoff_delay(attempt=4, retry_after=5.0)
    assert delay2 == 16.0

  def test_custom_base_and_multiplier(self):
    """Custom base delays and backoff factors should scale accordingly."""
    delays = [
        calculate_backoff_delay(attempt=i, base_delay=0.5, backoff_factor=3.0)
        for i in range(4)
    ]
    assert delays == [0.5, 1.5, 4.5, 13.5]

  def test_jitter_variation(self):
    """When jitter=True, returned delay should be between nominal and nominal * (1 + jitter_factor)."""
    nominal = 4.0
    delays = [
        calculate_backoff_delay(attempt=2, jitter=True, jitter_factor=0.2)
        for _ in range(50)
    ]
    for d in delays:
      assert nominal <= d <= nominal * 1.2


class TestErrorClassification:
  """Tests for classifying retryable vs non-retryable errors."""

  def test_retryable_status_codes(self):
    """429 and 5xx status codes should be classified as retryable."""
    for status in [429, 500, 502, 503, 504, 520, 599]:
      err = HTTPError(status, f"HTTP Error {status}", b"Error body", {})
      is_retriable, code, _ = is_retryable_error(err)
      assert is_retriable is True, f"Status {status} should be retryable"
      assert code == status

  def test_non_retryable_client_errors(self):
    """4xx errors (except 429) must fail fast without retry."""
    for status in [400, 401, 403, 404, 405, 413, 415, 422]:
      err = HTTPError(status, f"HTTP Error {status}", b"Client error", {})
      is_retriable, code, _ = is_retryable_error(err)
      assert is_retriable is False, f"Status {status} should NOT be retryable"
      assert code == status

  def test_network_and_connection_exceptions(self):
    """Network drops, socket timeouts, and connection resets should be retryable."""
    network_errors = [
        ConnectionResetError("Connection reset by peer"),
        ConnectionRefusedError("Connection refused"),
        TimeoutError("Request timed out"),
        OSError("Network is unreachable"),
        IOError("Socket closed unexpectedly"),
    ]
    for err in network_errors:
      is_retriable, _, _ = is_retryable_error(err)
      assert is_retriable is True, f"{err.__class__.__name__} should be retryable"

  def test_retry_after_header_extraction(self):
    """Retry-After header from 429 response should be correctly extracted."""
    err = HTTPError(429, "Too Many Requests", b"Rate limit", {"Retry-After": "7.5"})
    is_retriable, code, retry_after = is_retryable_error(err)
    assert is_retriable is True
    assert code == 429
    assert retry_after == 7.5


class TestSendGridDispatcherRetryLifecycle:
  """Unit tests for SendGridDispatcher retry behavior across execution lifecycles."""

  def test_immediate_success_first_attempt(self):
    """Email sent on first attempt without retries."""
    mock_client = MockSendGridClient([MockSendGridResponse(202)])
    sleep_calls: List[float] = []

    async def fake_sleep(d: float):
      sleep_calls.append(d)

    result = asyncio.run(
        send_audit_report(
            recipient_email="test@example.com",
            quiz_date="2026-08-22",
            total_questions=5,
            approved_count=5,
            failed_questions=[],
            client=mock_client,
            sleep_fn=fake_sleep,
            api_key="SG.test_key",
        )
    )

    assert result["status"] == "sent"
    assert result["status_code"] == 202
    assert result["attempts"] == 1
    assert result["retries"] == 0
    assert result["retry_history"] == []
    assert len(sleep_calls) == 0
    assert mock_client.call_count == 1

  def test_success_after_single_transient_failure(self):
    """Retry 1 succeeds after transient 503 Service Unavailable."""
    err_503 = HTTPError(503, "Service Unavailable", b"Down", {})
    mock_client = MockSendGridClient([err_503, MockSendGridResponse(200)])
    sleep_calls: List[float] = []

    async def fake_sleep(d: float):
      sleep_calls.append(d)

    result = asyncio.run(
        send_audit_report(
            recipient_email="test@example.com",
            quiz_date="2026-08-22",
            total_questions=5,
            approved_count=3,
            failed_questions=[{"question": "Q1", "review": "Failed"}],
            client=mock_client,
            sleep_fn=fake_sleep,
            api_key="SG.test_key",
        )
    )

    assert result["status"] == "sent"
    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert result["retries"] == 1
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 1.0  # 1st retry interval = 1.0s
    assert len(result["retry_history"]) == 1
    assert result["retry_history"][0]["status_code"] == 503
    assert result["retry_history"][0]["delay_seconds"] == 1.0

  def test_success_after_multiple_transient_failures(self):
    """Succeeds on attempt 4 after 3 transient errors (500, 429, 502)."""
    mock_client = MockSendGridClient([
        HTTPError(500, "Internal Server Error", b"", {}),
        HTTPError(429, "Too Many Requests", b"", {}),
        HTTPError(502, "Bad Gateway", b"", {}),
        MockSendGridResponse(202),
    ])
    sleep_calls: List[float] = []

    async def fake_sleep(d: float):
      sleep_calls.append(d)

    result = asyncio.run(
        send_audit_report(
            recipient_email="test@example.com",
            quiz_date="2026-08-22",
            total_questions=10,
            approved_count=8,
            failed_questions=[{"question": "Q1", "review": "Review note"}],
            client=mock_client,
            sleep_fn=fake_sleep,
            api_key="SG.test_key",
        )
    )

    assert result["status"] == "sent"
    assert result["attempts"] == 4
    assert result["retries"] == 3
    assert sleep_calls == [1.0, 2.0, 4.0]  # Exact exponential progression
    assert mock_client.call_count == 4
    assert len(result["retry_history"]) == 3

  def test_exhaustion_of_max_5_retries_during_outage(self):
    """When API is completely down, retries max 5 times with 1s, 2s, 4s, 8s, 16s intervals."""
    mock_client = MockSendGridClient([
        HTTPError(503, "Service Unavailable", b"Outage", {}),
        HTTPError(503, "Service Unavailable", b"Outage", {}),
        HTTPError(503, "Service Unavailable", b"Outage", {}),
        HTTPError(503, "Service Unavailable", b"Outage", {}),
        HTTPError(503, "Service Unavailable", b"Outage", {}),
        HTTPError(503, "Service Unavailable", b"Outage", {}),
    ])
    sleep_calls: List[float] = []

    async def fake_sleep(d: float):
      sleep_calls.append(d)

    result = asyncio.run(
        send_audit_report(
            recipient_email="alert@quizzy-news.com",
            quiz_date="2026-08-22",
            total_questions=5,
            approved_count=2,
            failed_questions=[{"question": "Q1", "review": "Failed"}],
            client=mock_client,
            sleep_fn=fake_sleep,
            api_key="SG.test_key",
        )
    )

    assert result["status"] == "error"
    assert result["attempts"] == 6  # 1 initial + 5 retries
    assert result["retries"] == 5
    assert sleep_calls == [1.0, 2.0, 4.0, 8.0, 16.0]  # Verified intervals
    assert mock_client.call_count == 6
    assert len(result["retry_history"]) == 6
    assert "Service Unavailable" in result["error"]

  def test_fail_fast_on_unauthorized_401(self):
    """Non-retryable 401 Unauthorized should fail immediately without retry storm."""
    err_401 = HTTPError(401, "Unauthorized", b"Invalid API key", {})
    mock_client = MockSendGridClient([err_401])
    sleep_calls: List[float] = []

    async def fake_sleep(d: float):
      sleep_calls.append(d)

    result = asyncio.run(
        send_audit_report(
            recipient_email="test@example.com",
            quiz_date="2026-08-22",
            total_questions=5,
            approved_count=5,
            failed_questions=[],
            client=mock_client,
            sleep_fn=fake_sleep,
            api_key="SG.bad_key",
        )
    )

    assert result["status"] == "error"
    assert result["status_code"] == 401
    assert result["attempts"] == 1
    assert result["retries"] == 0
    assert len(sleep_calls) == 0  # No sleep on non-retryable error
    assert mock_client.call_count == 1

  def test_dry_run_mode(self):
    """Dry-run mode returns preview without invoking SendGrid client."""
    result = asyncio.run(
        send_audit_report(
            recipient_email="test@example.com",
            quiz_date="2026-08-22",
            total_questions=5,
            approved_count=4,
            failed_questions=[{"question": "Q5", "review": "Grammar"}],
            dry_run=True,
        )
    )

    assert result["status"] == "dry_run"
    assert result["recipient"] == "test@example.com"
    assert result["quiz_date"] == "2026-08-22"
    assert result["approved"] == 4
    assert result["total"] == 5
    assert result["failed_count"] == 1
    assert "html_preview" in result


class TestOutageSimulationAndStormPrevention:
  """Simulate multi-worker outage scenarios to ensure no retry storms occur."""

  def test_concurrent_dispatches_during_outage(self):
    """Simulate 10 concurrent background dispatch tasks during an extended outage."""
    worker_count = 10
    sleep_log: Dict[int, List[float]] = {}
    mock_clients = {}

    for i in range(worker_count):
      sleep_log[i] = []
      # Every worker encounters persistent 503 errors
      mock_clients[i] = MockSendGridClient([
          HTTPError(503, "Service Unavailable", b"", {}) for _ in range(10)
      ])

    async def run_worker(worker_id: int):
      async def worker_sleep(d: float):
        sleep_log[worker_id].append(d)

      return await send_audit_report(
          recipient_email=f"worker{worker_id}@example.com",
          quiz_date="2026-08-22",
          total_questions=5,
          approved_count=3,
          failed_questions=[{"question": "Q1", "review": "Fail"}],
          client=mock_clients[worker_id],
          sleep_fn=worker_sleep,
          api_key="SG.test_key",
      )

    async def run_all():
      return await asyncio.gather(*(run_worker(i) for i in range(worker_count)))

    results = asyncio.run(run_all())

    # Verify all 10 workers behaved identically and safely
    total_api_calls = sum(mock_clients[i].call_count for i in range(worker_count))
    assert total_api_calls == 10 * 6  # 60 calls total, strictly bounded

    for i, res in enumerate(results):
      assert res["status"] == "error"
      assert res["attempts"] == 6
      assert res["retries"] == 5
      assert sleep_log[i] == [1.0, 2.0, 4.0, 8.0, 16.0]

  def test_rate_limit_429_with_retry_after_coordination(self):
    """When SendGrid returns 429 with Retry-After header, backoff conforms to header."""
    mock_client = MockSendGridClient([
        HTTPError(429, "Too Many Requests", b"Slow down", {"Retry-After": "6.0"}),
        MockSendGridResponse(202),
    ])
    sleep_calls: List[float] = []

    async def fake_sleep(d: float):
      sleep_calls.append(d)

    result = asyncio.run(
        send_audit_report(
            recipient_email="test@example.com",
            quiz_date="2026-08-22",
            total_questions=5,
            approved_count=5,
            failed_questions=[],
            client=mock_client,
            sleep_fn=fake_sleep,
            api_key="SG.test_key",
        )
    )

    assert result["status"] == "sent"
    assert result["attempts"] == 2
    assert result["retries"] == 1
    assert sleep_calls == [6.0]  # Respected Retry-After: 6.0s instead of base 1.0s


class TestHTMLReportBuilder:
  """Verify HTML audit report layout and structure."""

  def test_report_with_failures(self):
    html = _build_html_report(
        quiz_date="2026-08-22",
        total=10,
        approved=8,
        failed_questions=[
            {"question": "What is 2+2?", "review": "Rule 1 violation"},
            {"question": "What is the capital?", "review": "Rule 2 violation"},
        ],
    )
    assert "Quizzy Auditor Report" in html
    assert "2026-08-22" in html
    assert "8" in html
    assert "2" in html
    assert "Failed Questions" in html
    assert "Rule 1 violation" in html
    assert "Rule 2 violation" in html

  def test_report_all_passed(self):
    html = _build_html_report(
        quiz_date="2026-08-22",
        total=5,
        approved=5,
        failed_questions=[],
    )
    assert "Quizzy Auditor Report" in html
    assert "5" in html
    assert "Failed Questions" not in html


class TestNoQuizAlert:
  """Verify the no-quiz alert email sent after retries are exhausted."""

  def test_dry_run_mode(self):
    """Dry-run mode returns preview without invoking SendGrid client."""
    result = asyncio.run(
        send_no_quiz_alert(
            recipient_email="test@example.com",
            quiz_date="2026-08-28",
            attempts=4,
            dry_run=True,
        )
    )

    assert result["status"] == "dry_run"
    assert result["recipient"] == "test@example.com"
    assert result["quiz_date"] == "2026-08-28"
    assert result["fetch_attempts"] == 4
    assert "html_preview" in result

  def test_sends_via_client(self):
    """Alert dispatches through the SendGrid client like a normal report."""
    mock_client = MockSendGridClient([MockSendGridResponse(202)])

    result = asyncio.run(
        send_no_quiz_alert(
            recipient_email="test@example.com",
            quiz_date="2026-08-28",
            attempts=4,
            client=mock_client,
        )
    )

    assert result["status"] == "sent"
    assert result["status_code"] == 202
    assert mock_client.call_count == 1


class TestReporterAgentIntegration:
  """Integration tests for ReporterAgent utilizing SendGrid dispatch with exponential backoff."""

  def test_reporter_agent_triggers_sendgrid_on_failed_questions(self):
    """ReporterAgent should invoke send_audit_report when audit results contain failed questions."""
    from daily_audit_pipeline.reporter import ReporterAgent
    from daily_audit_pipeline.schemas import QuestionAudit

    agent = ReporterAgent(name="test_reporter")
    
    # Setup mock session and invocation context
    mock_session = MagicMock()
    mock_session.state = {
        "audit_results": [
            {"question": "Q1: Valid?", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
            {"question": "Q2: Broken?", "choices": ["X", "Y", "Z"], "answer_matches_choice": False, "approved": False, "review": "Rule 1 violated"},
        ]
    }
    
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    mock_ctx.invocation_id = "test-inv-123"
    mock_ctx.branch = None

    with patch("daily_audit_pipeline.reporter.firestore.Client") as mock_firestore:
      mock_db = MagicMock()
      mock_firestore.return_value = mock_db

      with patch("daily_audit_pipeline.reporter.send_audit_report", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {
            "status": "sent",
            "status_code": 202,
            "attempts": 1,
            "retries": 0,
            "retry_history": [],
        }

        async def run_agent():
          events = []
          async for event in agent._run_async_impl(mock_ctx):
            events.append(event)
          return events

        events = asyncio.run(run_agent())

        assert len(events) == 1
        assert "Report" in events[0].content.parts[0].text
        assert "SendGrid status: sent" in events[0].content.parts[0].text
        assert mock_send.await_count == 1
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["total_questions"] == 2
        assert call_kwargs["approved_count"] == 1
        assert len(call_kwargs["failed_questions"]) == 1
        assert call_kwargs["failed_questions"][0]["question"] == "Q2: Broken?"

  def test_reporter_agent_skips_sendgrid_when_all_questions_approved(self):
    """ReporterAgent should not invoke send_audit_report when all questions pass."""
    from daily_audit_pipeline.reporter import ReporterAgent

    agent = ReporterAgent(name="test_reporter")
    mock_session = MagicMock()
    mock_session.state = {
        "audit_results": [
            {"question": "Q1", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
            {"question": "Q2", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
        ]
    }
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session
    mock_ctx.invocation_id = "test-inv-456"
    mock_ctx.branch = None

    with patch("daily_audit_pipeline.reporter.firestore.Client"):
      with patch("daily_audit_pipeline.reporter.send_audit_report", new_callable=AsyncMock) as mock_send:
        async def run_agent():
          events = []
          async for event in agent._run_async_impl(mock_ctx):
            events.append(event)
          return events

        events = asyncio.run(run_agent())

        assert len(events) == 1
        assert "All questions approved — no email sent." in events[0].content.parts[0].text
        assert mock_send.await_count == 0
