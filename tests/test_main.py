"""Unit tests for FastAPI endpoints in main.py."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.testclient import TestClient

import auth
from main import app


@pytest.fixture
def client():
  return TestClient(app)


def test_health_check(client):
  """GET /health returns healthy status."""
  response = client.get("/health")
  assert response.status_code == 200
  assert response.json() == {"status": "healthy"}


def test_dashboard_rendering(client):
  """GET / renders HTML dashboard with audit summary."""
  mock_doc = MagicMock()
  mock_doc.to_dict.return_value = {
      "quiz_date": "2026-08-22",
      "total_questions": 5,
      "approved": 4,
      "failed": 1,
  }

  mock_db = MagicMock()
  mock_db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = [mock_doc]

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/")
    assert response.status_code == 200
    assert "Quizzy Auditor Dashboard" in response.text
    assert "2026-08-22" in response.text


def test_report_detail_found(client):
  """GET /reports/{date} renders HTML detail view for existing report."""
  mock_doc = MagicMock()
  mock_doc.exists = True
  mock_doc.to_dict.return_value = {
      "quizDate": "2026-08-22",
      "total_questions": 5,
      "approved": 4,
      "failed": 1,
      "questions": [
          {"question": "What is 2+2?", "approved": True, "review": ""},
          {"question": "According to the article...", "approved": False, "review": "Rule 1 failure"},
      ],
  }

  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/reports/2026-08-22")
    assert response.status_code == 200
    assert "Audit — 2026-08-22" in response.text
    assert "Rule 1 failure" in response.text


def test_report_detail_no_quiz_still_retrying(client):
  """GET /reports/{date} distinguishes 'nothing fetched yet' from 'all passed'."""
  mock_doc = MagicMock()
  mock_doc.exists = True
  mock_doc.to_dict.return_value = {
      "quizDate": "2026-08-28",
      "status": "empty",
      "fetchAttempts": 2,
      "questions": [],
      "summary": {"total": 0, "approved": 0, "failed": 0},
  }

  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/reports/2026-08-28")
    assert response.status_code == 200
    assert "No quiz was available to audit yet" in response.text
    assert "attempt 2/4" in response.text
    assert "All questions passed — nothing to review." not in response.text


def test_report_detail_no_quiz_final_alert(client):
  """GET /reports/{date} shows the alerted state once retries are exhausted."""
  mock_doc = MagicMock()
  mock_doc.exists = True
  mock_doc.to_dict.return_value = {
      "quizDate": "2026-08-28",
      "status": "empty_final",
      "fetchAttempts": 4,
      "questions": [],
      "summary": {"total": 0, "approved": 0, "failed": 0},
  }

  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/reports/2026-08-28")
    assert response.status_code == 200
    assert "The team has been alerted" in response.text


def test_dashboard_no_quiz_badge(client):
  """GET / shows a neutral NO QUIZ badge, not PASS, for a zero-question day."""
  mock_doc = MagicMock()
  mock_doc.to_dict.return_value = {
      "quizDate": "2026-08-28",
      "status": "empty_final",
      "summary": {"total": 0, "approved": 0, "failed": 0},
  }

  mock_db = MagicMock()
  mock_db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = [mock_doc]

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/")
    assert response.status_code == 200
    assert '<span class="badge neutral">NO QUIZ</span>' in response.text
    assert '<span class="badge good">PASS</span>' not in response.text


def test_report_detail_not_found(client):
  """GET /reports/{date} returns 404 when report does not exist."""
  mock_doc = MagicMock()
  mock_doc.exists = False

  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/reports/2026-01-01")
    assert response.status_code == 404


def test_trigger_audit_unauthorized_without_token(client):
  """POST /trigger-audit returns 401 when unauthenticated and SKIP_AUTH is not set."""
  with patch.dict("os.environ", {"SKIP_AUTH": "false"}):
    response = client.post("/trigger-audit")
    assert response.status_code == 401


def test_trigger_audit_skips_when_already_complete(client):
  """POST /trigger-audit is a no-op once today's audit is resolved, so the
  hourly retry schedule doesn't re-run the pipeline (or spend Gemini/search
  budget) after a real report — or a final no-quiz alert — is saved."""
  mock_doc = MagicMock()
  mock_doc.exists = True
  mock_doc.to_dict.return_value = {"status": "complete"}

  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch.dict("os.environ", {"SKIP_AUTH": "true"}):
    with patch("main.get_db", return_value=mock_db):
      with patch("main.Runner") as mock_runner_cls:
        response = client.post("/trigger-audit")
        assert response.status_code == 200
        assert response.json()["status"] == "skipped"
        mock_runner_cls.assert_not_called()


def test_trigger_audit_uses_saved_rule_instruction(client):
  """POST /trigger-audit builds the pipeline with the saved config/auditor_rules
  instruction, not the hardcoded default, when one has been saved."""
  mock_audit_doc = MagicMock()
  mock_audit_doc.exists = False  # no audits/{today} doc yet -> not skipped

  mock_rules_doc = MagicMock()
  mock_rules_doc.exists = True
  mock_rules_doc.to_dict.return_value = {"instruction": "CUSTOM RUBRIC TEXT"}

  mock_db = MagicMock()

  def collection_side_effect(name):
    coll = MagicMock()
    if name == "audits":
      coll.document.return_value.get.return_value = mock_audit_doc
    elif name == "config":
      coll.document.return_value.get.return_value = mock_rules_doc
    return coll

  mock_db.collection.side_effect = collection_side_effect

  with patch.dict("os.environ", {"SKIP_AUTH": "true"}):
    with patch("main.get_db", return_value=mock_db):
      with patch("main.build_daily_pipeline") as mock_build_pipeline:
        with patch("main.Runner") as mock_runner_cls:
          mock_runner = MagicMock()

          async def fake_run_async(**kwargs):
            if False:
              yield None

          mock_runner.run_async = fake_run_async
          mock_runner_cls.return_value = mock_runner

          response = client.post("/trigger-audit")

          assert response.status_code == 200
          mock_build_pipeline.assert_called_once_with("CUSTOM RUBRIC TEXT")


def test_admin_login_valid_token_sets_cookie(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  with patch("main.auth.verify_google_id_token", return_value="donovanuy@gmail.com"):
    response = client.post("/admin/login", json={"credential": "fake-id-token"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "email": "donovanuy@gmail.com"}
    assert "admin_session" in response.cookies

    set_cookie_header = response.headers.get("set-cookie")
    assert "HttpOnly" in set_cookie_header
    assert "Secure" in set_cookie_header
    assert "SameSite=strict" in set_cookie_header
    assert f"Max-Age={auth.SESSION_TTL_SECONDS}" in set_cookie_header


def test_admin_login_invalid_token_returns_401(client):
  with patch("main.auth.verify_google_id_token", side_effect=ValueError("bad token")):
    response = client.post("/admin/login", json={"credential": "bad-token"})
    assert response.status_code == 401


def test_admin_logout_clears_cookie(client):
  response = client.post("/admin/logout")
  assert response.status_code == 200


def _admin_cookie_header():
  """A valid signed admin session cookie header for tests, using the same
  SESSION_SECRET the test process sets via monkeypatch."""
  import auth
  cookie_value = auth.create_session_cookie("donovanuy@gmail.com", secret="test-secret")
  return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={cookie_value}"}


def test_admin_rules_page_requires_auth(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  response = client.get("/admin")
  assert response.status_code == 401


def test_admin_rules_page_shows_current_instruction(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_rules_doc = MagicMock()
  mock_rules_doc.exists = True
  mock_rules_doc.to_dict.return_value = {"instruction": "CURRENT RUBRIC TEXT"}
  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_rules_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/admin", headers=_admin_cookie_header())
    assert response.status_code == 200
    assert "CURRENT RUBRIC TEXT" in response.text


def test_admin_save_rules_requires_auth(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  response = client.post("/admin/rules", data={"instruction": "x", "action": "save"})
  assert response.status_code == 401


def test_admin_save_rules_writes_and_logs_revision(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_existing_doc = MagicMock()
  mock_existing_doc.exists = False
  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_existing_doc

  with patch("main.get_db", return_value=mock_db):
    with patch("main.log_revision") as mock_log_revision:
      response = client.post(
          "/admin/rules",
          data={"instruction": "NEW RUBRIC TEXT", "action": "save"},
          headers=_admin_cookie_header(),
      )
      assert response.status_code == 200
      assert "Rules saved" in response.text

      set_call = mock_db.collection.return_value.document.return_value.set.call_args.args[0]
      assert set_call["instruction"] == "NEW RUBRIC TEXT"
      assert set_call["updatedBy"] == "donovanuy@gmail.com"

      mock_log_revision.assert_called_once()
      call_kwargs = mock_log_revision.call_args.kwargs
      assert call_kwargs["revision_type"] == "rule_change"
      assert call_kwargs["actor"] == "donovanuy@gmail.com"


def test_admin_dry_run_today_returns_results_without_saving(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_db = MagicMock()

  with patch("main.get_db", return_value=mock_db):
    with patch("main.fetch_quiz", new_callable=AsyncMock) as mock_fetch:
      mock_fetch.return_value = {
          "quizDate": "2026-08-30",
          "quiz": [{"question": "Q1?", "choices": ["A", "B", "C"], "answer": "A", "source": {"url": "https://example.com"}}],
      }
      with patch("main.InMemorySessionService") as mock_session_service_cls:
        mock_session_service = MagicMock()

        async def fake_create_session(**kwargs):
          return MagicMock(id="dry-run-session-1")

        mock_session_service.create_session = fake_create_session

        async def fake_get_session(**kwargs):
          session = MagicMock()
          session.state = {
              "audit_results": [
                  {
                      "question": "Q1?",
                      "choices": ["A", "B", "C"],
                      "answer_matches_choice": True,
                      "approved": True,
                      "review": "Looks good.",
                      "sources_checked": [],
                  }
              ]
          }
          return session

        mock_session_service.get_session = fake_get_session
        mock_session_service_cls.return_value = mock_session_service

        with patch("main.Runner") as mock_runner_cls:
          mock_runner = MagicMock()

          async def fake_run_async(**kwargs):
            if False:
              yield None

          mock_runner.run_async = fake_run_async
          mock_runner_cls.return_value = mock_runner

          response = client.post(
              "/admin/rules",
              data={"instruction": "DRAFT INSTRUCTION", "action": "dry_run", "target": "today"},
              headers=_admin_cookie_header(),
          )

          assert response.status_code == 200
          assert "1/1 approved" in response.text
          # Dry run never writes to audits/{date}
          for call in mock_db.collection.call_args_list:
            assert call.args[0] != "audits"


def test_dashboard_shows_sign_in_when_anonymous(client):
  mock_db = MagicMock()
  mock_db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = []

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/")
    assert response.status_code == 200
    assert "g_id_signin" in response.text
    assert 'href="/admin"' not in response.text


def test_dashboard_shows_admin_nav_when_authenticated(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_db = MagicMock()
  mock_db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = []

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/", headers=_admin_cookie_header())
    assert response.status_code == 200
    assert 'href="/admin"' in response.text


def test_report_detail_shows_all_questions_with_choices_no_answer(client):
  mock_doc = MagicMock()
  mock_doc.exists = True
  mock_doc.to_dict.return_value = {
      "quizDate": "2026-08-30",
      "status": "complete",
      "summary": {"total": 2, "approved": 1, "failed": 1},
      "questions": [
          {
              "question": "What is the capital of France?",
              "choices": ["Paris", "London", "Berlin"],
              "answer_matches_choice": True,
              "approved": True,
              "review": "Answer matches a choice.",
              "sources_checked": [{"url": "https://example.com/paris", "verified": True}],
          },
          {
              "question": "According to the article, what happened?",
              "choices": ["X", "Y", "Z"],
              "answer_matches_choice": False,
              "approved": False,
              "review": "Rule 1 failure: meta-referential phrasing.",
              "sources_checked": [{"url": "https://fabricated.example/x", "verified": False}],
          },
      ],
  }
  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/reports/2026-08-30")
    assert response.status_code == 200
    # Both questions shown, not just the failed one
    assert "What is the capital of France?" in response.text
    assert "According to the article, what happened?" in response.text
    # Choices shown
    assert "Paris" in response.text
    assert "London" in response.text
    # Sources and their verification status shown
    assert "https://example.com/paris" in response.text
    assert "https://fabricated.example/x" in response.text
    assert "unverified" in response.text
