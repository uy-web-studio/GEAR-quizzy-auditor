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
    assert "Audit Report for 2026-08-22" in response.text
    assert "Rule 1 failure" in response.text


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
