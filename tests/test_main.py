"""Unit tests for FastAPI endpoints in main.py."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.testclient import TestClient

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


def test_admin_login_invalid_token_returns_401(client):
  with patch("main.auth.verify_google_id_token", side_effect=ValueError("bad token")):
    response = client.post("/admin/login", json={"credential": "bad-token"})
    assert response.status_code == 401


def test_admin_logout_clears_cookie(client):
  response = client.post("/admin/logout")
  assert response.status_code == 200
