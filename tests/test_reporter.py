"""Unit tests for ReporterAgent."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions.session import Session
from daily_audit_pipeline.reporter import ReporterAgent
from daily_audit_pipeline.schemas import QuestionAudit


class TestReporterAgent:
  """Tests for ReporterAgent lifecycle and event emission."""

  @pytest.mark.anyio
  async def test_reporter_agent_all_approved_no_email(self):
    """When all questions pass, reporter writes Firestore doc and skips email."""
    agent = ReporterAgent(name="reporter_agent")
    
    session = MagicMock(spec=Session)
    session.state = {
        "audit_results": [
            {"question": "Q1", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
            {"question": "Q2", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
        ]
    }
    
    ctx = MagicMock(spec=InvocationContext)
    ctx.session = session
    ctx.invocation_id = "test-inv-1"
    ctx.branch = "main"

    with patch("daily_audit_pipeline.reporter.send_audit_report") as mock_send:
      with patch("google.cloud.firestore.Client") as mock_firestore:
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db
        
        events = []
        async for event in agent._run_async_impl(ctx):
          events.append(event)
        
        # Email should NOT be sent when all questions pass
        mock_send.assert_not_called()
        
        # Event was yielded with summary
        assert len(events) == 1
        summary_text = events[0].content.parts[0].text
        assert "Total: 2" in summary_text
        assert "Approved: 2" in summary_text
        assert "Failed: 0" in summary_text
        assert "All questions approved — no email sent." in summary_text

  @pytest.mark.anyio
  async def test_reporter_agent_with_failures_triggers_email(self):
    """When questions fail, reporter calls send_audit_report."""
    agent = ReporterAgent(name="reporter_agent")
    
    session = MagicMock(spec=Session)
    session.state = {
        "audit_results": [
            QuestionAudit(question="Q1", choices=["A", "B", "C"], answer_matches_choice=True, approved=True, review="Looks good."),
            QuestionAudit(question="Q2", choices=["X", "Y", "Z"], answer_matches_choice=False, approved=False, review="Rule 1 failure"),
        ]
    }
    
    ctx = MagicMock(spec=InvocationContext)
    ctx.session = session
    ctx.invocation_id = "test-inv-2"
    ctx.branch = "main"

    with patch("daily_audit_pipeline.reporter.send_audit_report", new_callable=AsyncMock) as mock_send:
      mock_send.return_value = {"status": "sent", "recipient": "test@example.com"}
      with patch("google.cloud.firestore.Client") as mock_firestore:
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db
        
        events = []
        async for event in agent._run_async_impl(ctx):
          events.append(event)
        
        # Email should be sent
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["total_questions"] == 2
        assert call_kwargs["approved_count"] == 1
        assert len(call_kwargs["failed_questions"]) == 1
        
        assert len(events) == 1
        summary_text = events[0].content.parts[0].text
        assert "Total: 2" in summary_text
        assert "Approved: 1" in summary_text
        assert "Failed: 1" in summary_text
        assert "SendGrid status: sent" in summary_text

  @pytest.mark.anyio
  async def test_reporter_agent_no_quiz_first_attempt_no_email(self):
    """When no questions were audited, first attempt writes an 'empty'
    report and does not email — it waits for the hourly retry."""
    agent = ReporterAgent(name="reporter_agent")

    session = MagicMock(spec=Session)
    session.state = {"audit_results": []}

    ctx = MagicMock(spec=InvocationContext)
    ctx.session = session
    ctx.invocation_id = "test-inv-3"
    ctx.branch = "main"

    with patch("daily_audit_pipeline.reporter.send_no_quiz_alert") as mock_alert:
      with patch("daily_audit_pipeline.reporter.firestore.Client") as mock_firestore:
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value.get.return_value.exists = False
        mock_firestore.return_value = mock_db

        events = []
        async for event in agent._run_async_impl(ctx):
          events.append(event)

        mock_alert.assert_not_called()
        set_call_doc = mock_db.collection.return_value.document.return_value.set.call_args.args[0]
        assert set_call_doc["status"] == "empty"
        assert set_call_doc["fetchAttempts"] == 1

        summary_text = events[0].content.parts[0].text
        assert "Fetch attempts: 1/4" in summary_text
        assert "will retry next hour, no email sent yet" in summary_text

  @pytest.mark.anyio
  async def test_reporter_agent_no_quiz_fourth_attempt_sends_alert(self):
    """After MAX_FETCH_ATTEMPTS consecutive empty checks, reporter alerts."""
    agent = ReporterAgent(name="reporter_agent")

    session = MagicMock(spec=Session)
    session.state = {"audit_results": []}

    ctx = MagicMock(spec=InvocationContext)
    ctx.session = session
    ctx.invocation_id = "test-inv-4"
    ctx.branch = "main"

    with patch(
        "daily_audit_pipeline.reporter.send_no_quiz_alert", new_callable=AsyncMock
    ) as mock_alert:
      mock_alert.return_value = {"status": "sent"}
      with patch("daily_audit_pipeline.reporter.firestore.Client") as mock_firestore:
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value.get.return_value.exists = True
        mock_db.collection.return_value.document.return_value.get.return_value.to_dict.return_value = {
            "fetchAttempts": 3
        }
        mock_firestore.return_value = mock_db

        events = []
        async for event in agent._run_async_impl(ctx):
          events.append(event)

        mock_alert.assert_awaited_once()
        call_kwargs = mock_alert.call_args.kwargs
        assert call_kwargs["attempts"] == 4

        set_call_doc = mock_db.collection.return_value.document.return_value.set.call_args.args[0]
        assert set_call_doc["status"] == "empty_final"
        assert set_call_doc["fetchAttempts"] == 4

        summary_text = events[0].content.parts[0].text
        assert "Fetch attempts: 4/4" in summary_text
        assert "alert sent" in summary_text
