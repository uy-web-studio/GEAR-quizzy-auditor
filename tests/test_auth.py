"""Unit tests for auth.py."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from auth import (
    SESSION_COOKIE_NAME,
    create_session_cookie,
    get_admin_email,
    get_allowed_admin_emails,
    require_admin,
    verify_google_id_token,
    verify_session_cookie,
)


class TestSessionCookie:
  def test_round_trip_valid(self):
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    assert verify_session_cookie(cookie, secret="test-secret") == "donovanuy@gmail.com"

  def test_tampered_signature_rejected(self):
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    tampered = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
    assert verify_session_cookie(tampered, secret="test-secret") is None

  def test_wrong_secret_rejected(self):
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    assert verify_session_cookie(cookie, secret="other-secret") is None

  def test_expired_cookie_rejected(self):
    with patch("auth.SESSION_TTL_SECONDS", -10):
      cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    assert verify_session_cookie(cookie, secret="test-secret") is None

  def test_malformed_cookie_rejected(self):
    assert verify_session_cookie("not-a-valid-cookie", secret="test-secret") is None


class TestAllowedAdminEmails:
  def test_defaults_to_donovan(self, monkeypatch):
    monkeypatch.delenv("ALLOWED_ADMIN_EMAILS", raising=False)
    assert get_allowed_admin_emails() == ["donovanuy@gmail.com"]

  def test_parses_comma_separated_list(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "a@example.com, b@example.com")
    assert get_allowed_admin_emails() == ["a@example.com", "b@example.com"]


class TestVerifyGoogleIdToken:
  def test_valid_token_for_allowed_email_returns_email(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
    with patch("auth.id_token.verify_oauth2_token") as mock_verify:
      mock_verify.return_value = {"email": "donovanuy@gmail.com", "email_verified": True}
      email = verify_google_id_token("fake-token", client_id="client-id-123")
    assert email == "donovanuy@gmail.com"

  def test_unverified_email_rejected(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
    with patch("auth.id_token.verify_oauth2_token") as mock_verify:
      mock_verify.return_value = {"email": "donovanuy@gmail.com", "email_verified": False}
      with pytest.raises(ValueError):
        verify_google_id_token("fake-token", client_id="client-id-123")

  def test_non_allowlisted_email_rejected(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
    with patch("auth.id_token.verify_oauth2_token") as mock_verify:
      mock_verify.return_value = {"email": "someone-else@gmail.com", "email_verified": True}
      with pytest.raises(ValueError):
        verify_google_id_token("fake-token", client_id="client-id-123")


class TestRequireAdmin:
  def test_valid_session_returns_email(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    request = MagicMock()
    request.cookies = {SESSION_COOKIE_NAME: cookie}
    assert require_admin(request) == "donovanuy@gmail.com"

  def test_missing_cookie_raises_401(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    request = MagicMock()
    request.cookies = {}
    with pytest.raises(HTTPException) as exc_info:
      require_admin(request)
    assert exc_info.value.status_code == 401

  def test_invalid_cookie_raises_401(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    request = MagicMock()
    request.cookies = {SESSION_COOKIE_NAME: "garbage"}
    with pytest.raises(HTTPException) as exc_info:
      require_admin(request)
    assert exc_info.value.status_code == 401


class TestGetAdminEmail:
  def test_returns_none_without_raising_when_missing(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    request = MagicMock()
    request.cookies = {}
    assert get_admin_email(request) is None
