"""Admin authentication: Google Sign-In verification and signed session
cookies. See docs/superpowers/specs/2026-08-30-admin-dashboard-design.md §4.
"""

import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

SESSION_COOKIE_NAME = "admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 hours


def get_allowed_admin_emails() -> list[str]:
  """Admin allowlist — one address today, a comma-separated list if more
  are ever added. See spec §4."""
  raw = os.environ.get("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
  return [email.strip() for email in raw.split(",") if email.strip()]


def get_session_secret() -> str:
  secret = os.environ.get("SESSION_SECRET")
  if not secret:
    raise RuntimeError("SESSION_SECRET is not configured")
  return secret


def verify_google_id_token(token: str, client_id: str) -> str:
  """Verify a Google Identity Services ID token and return the verified
  email. Raises ValueError if invalid or the email isn't allowlisted."""
  claims = id_token.verify_oauth2_token(
      token, google_requests.Request(), audience=client_id
  )
  if not claims.get("email_verified"):
    raise ValueError("Email not verified by Google")
  email = claims.get("email")
  if email not in get_allowed_admin_emails():
    raise ValueError(f"{email} is not an allowed admin")
  return email


def create_session_cookie(email: str, secret: Optional[str] = None) -> str:
  """Build a signed, stateless session token: email:expiry:hmac_signature."""
  secret = secret or get_session_secret()
  expiry = int(time.time()) + SESSION_TTL_SECONDS
  payload = f"{email}:{expiry}"
  signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
  return f"{payload}:{signature}"


def verify_session_cookie(cookie_value: str, secret: Optional[str] = None) -> Optional[str]:
  """Verify a session cookie's signature and expiry. Returns the verified
  email, or None if invalid/expired/tampered."""
  secret = secret or get_session_secret()
  try:
    email, expiry_str, signature = cookie_value.rsplit(":", 2)
  except ValueError:
    return None

  payload = f"{email}:{expiry_str}"
  expected_signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
  if not hmac.compare_digest(signature, expected_signature):
    return None

  try:
    if int(expiry_str) < int(time.time()):
      return None
  except ValueError:
    return None

  return email


def get_admin_email(request: Request) -> Optional[str]:
  """Return the verified admin email for this request's session cookie, or
  None if there isn't a valid one. Non-raising — for conditionally showing
  admin UI to anonymous visitors without gating the route."""
  cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
  if not cookie_value:
    return None
  try:
    return verify_session_cookie(cookie_value)
  except RuntimeError:
    # SESSION_SECRET unconfigured. This function is used un-gated on public
    # pages to decide whether to show an admin nav link, so it must fail
    # open (display-only) rather than 500ing public traffic. require_admin
    # below still fails loud for a genuine misconfiguration by checking
    # get_session_secret() directly, independent of this catch.
    return None


def require_admin(request: Request) -> str:
  """FastAPI dependency: raises 401 if there's no valid admin session,
  otherwise returns the verified email."""
  get_session_secret()  # Raises RuntimeError (-> 500) if SESSION_SECRET is
                         # unset. Checked directly so a real deployment
                         # misconfiguration still fails loud here, even
                         # though get_admin_email fails open (returns None)
                         # for its own public-page callers.
  email = get_admin_email(request)
  if email is None:
    raise HTTPException(status_code=401, detail="Admin sign-in required")
  return email
