import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import logging
import os
import random
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from google.cloud import secretmanager
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Content, Email, From, Mail, Personalization, To

logger = logging.getLogger(__name__)

# Default configuration for exponential backoff retry strategy
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_BASE_DELAY: float = 1.0
DEFAULT_BACKOFF_FACTOR: float = 2.0
DEFAULT_MAX_DELAY: float = 32.0

# HTTP status codes eligible for retry
DEFAULT_RETRYABLE_STATUS_CODES: Set[int] = {429, 500, 502, 503, 504}


@dataclass
class RetryConfig:
  """Configuration for SendGrid exponential backoff retry policy."""

  max_retries: int = DEFAULT_MAX_RETRIES
  base_delay: float = DEFAULT_BASE_DELAY
  backoff_factor: float = DEFAULT_BACKOFF_FACTOR
  max_delay: float = DEFAULT_MAX_DELAY
  jitter: bool = False
  jitter_factor: float = 0.1
  retryable_status_codes: Set[int] = field(
      default_factory=lambda: set(DEFAULT_RETRYABLE_STATUS_CODES)
  )


def calculate_backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: bool = False,
    jitter_factor: float = 0.1,
    retry_after: Optional[float] = None,
) -> float:
  """Calculate exponential backoff delay for a given retry attempt (0-indexed).

  Default progression for base=1.0, factor=2.0:
    - Attempt 0 (Retry 1): 1.0s
    - Attempt 1 (Retry 2): 2.0s
    - Attempt 2 (Retry 3): 4.0s
    - Attempt 3 (Retry 4): 8.0s
    - Attempt 4 (Retry 5): 16.0s

  Args:
    attempt: 0-indexed retry attempt index (0 for first retry).
    base_delay: Base delay in seconds.
    backoff_factor: Exponential multiplier factor.
    max_delay: Maximum delay cap in seconds.
    jitter: Whether to apply randomized jitter to prevent thundering herds.
    jitter_factor: Maximum fraction of jitter to add if jitter is enabled.
    retry_after: Explicit delay in seconds if provided by server (e.g.
      Retry-After header).

  Returns:
    Calculated delay in seconds.
  """
  if attempt < 0:
    attempt = 0

  delay = base_delay * (backoff_factor**attempt)
  if max_delay > 0:
    delay = min(delay, max_delay)

  if retry_after is not None and retry_after > 0:
    delay = max(delay, retry_after)

  if jitter and delay > 0:
    delay += random.uniform(0, jitter_factor * delay)

  return max(0.0, delay)


def is_retryable_error(
    exc: Exception,
    retryable_status_codes: Optional[Set[int]] = None,
) -> Tuple[bool, Optional[int], Optional[float]]:
  """Determine whether an error encountered during dispatch is retryable.

  Args:
    exc: Caught exception.
    retryable_status_codes: Set of HTTP status codes considered retryable.

  Returns:
    Tuple of (is_retryable: bool, status_code: Optional[int], retry_after:
    Optional[float])
  """
  if retryable_status_codes is None:
    retryable_status_codes = DEFAULT_RETRYABLE_STATUS_CODES

  status_code: Optional[int] = None
  retry_after: Optional[float] = None

  # Extract status_code from exception
  if hasattr(exc, "status_code") and isinstance(exc.status_code, int):
    status_code = exc.status_code
  elif hasattr(exc, "code") and isinstance(exc.code, int):
    status_code = exc.code
  elif hasattr(exc, "response") and hasattr(exc.response, "status_code"):
    status_code = exc.response.status_code

  # Extract Retry-After header if present
  headers = getattr(exc, "headers", None)
  if headers and isinstance(headers, dict):
    for k, v in headers.items():
      if k.lower() == "retry-after":
        try:
          retry_after = float(v)
        except (ValueError, TypeError):
          pass

  # If an HTTP status code is present:
  if status_code is not None:
    if status_code in retryable_status_codes or status_code >= 500:
      return True, status_code, retry_after
    # Client errors 4xx (except 429) are non-retryable
    if 400 <= status_code < 500:
      return False, status_code, retry_after

  # Network, connection, and timeout errors are retryable
  exc_name = exc.__class__.__name__.lower()
  if any(
      term in exc_name
      for term in [
          "timeout",
          "connection",
          "connect",
          "network",
          "reset",
          "closed",
          "unavailable",
      ]
  ):
    return True, status_code, retry_after

  if isinstance(
      exc,
      (
          TimeoutError,
          ConnectionError,
          OSError,
          IOError,
      ),
  ):
    return True, status_code, retry_after

  # Default: treat unexpected runtime exceptions as retryable
  return True, status_code, retry_after


async def get_sendgrid_api_key() -> str:
  """Retrieve SendGrid API key from Secret Manager or environment variable.

  Expected secret name: sendgrid-api-key in the project's Secret Manager.
  Falls back to SENDGRID_API_KEY env var if secret doesn't exist.
  """
  try:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
      raise ValueError("GOOGLE_CLOUD_PROJECT not set")

    client = secretmanager.SecretManagerServiceClient()
    secret_name = (
        f"projects/{project_id}/secrets/sendgrid-api-key/versions/latest"
    )
    response = client.access_secret_version(request={"name": secret_name})
    return response.payload.data.decode("UTF-8")
  except Exception:
    # Fallback to environment variable
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
      raise ValueError(
          "SendGrid API key not found in Secret Manager or SENDGRID_API_KEY env"
          " var"
      )
    return api_key


class SendGridDispatcher:
  """SendGrid email dispatcher implementing exponential backoff retry strategy."""

  def __init__(
      self,
      api_key: Optional[str] = None,
      retry_config: Optional[RetryConfig] = None,
      client: Optional[Any] = None,
      sleep_fn: Optional[Callable[[float], Any]] = None,
  ):
    self.api_key = api_key
    self.retry_config = retry_config or RetryConfig()
    self.client = client
    self.sleep_fn = sleep_fn or asyncio.sleep

  async def get_client(self) -> Any:
    """Get or initialize the SendGrid API client."""
    if self.client is not None:
      return self.client
    api_key = self.api_key or await get_sendgrid_api_key()
    return SendGridAPIClient(api_key)

  async def dispatch_mail_with_retry(
      self,
      mail: Mail,
      recipient_email: str,
      subject: str,
      metadata: Optional[Dict[str, Any]] = None,
  ) -> Dict[str, Any]:
    """Dispatch email using exponential backoff retry strategy.

    Retries up to max_retries times with exponential backoff on 429, 5xx,
    and network errors. Fails immediately on 4xx client errors without retry storms.

    Args:
      mail: SendGrid Mail object to send.
      recipient_email: Target recipient email address.
      subject: Email subject.
      metadata: Optional additional metadata to include in response dictionary.

    Returns:
      Dictionary containing status, status_code, attempts, retries, retry_history,
      duration, and metadata.
    """
    sg_client = await self.get_client()
    max_retries = self.retry_config.max_retries
    retry_history: List[Dict[str, Any]] = []
    start_time = datetime.now(timezone.utc)

    for attempt in range(max_retries + 1):
      attempt_num = attempt + 1
      try:
        logger.info(
            "SendGrid dispatch attempt %d/%d to %s",
            attempt_num,
            max_retries + 1,
            recipient_email,
        )

        # Handle both async and sync client send methods
        if hasattr(sg_client, "send") and inspect.iscoroutinefunction(
            sg_client.send
        ):
          response = await sg_client.send(mail)
        elif hasattr(sg_client, "send"):
          response = await asyncio.to_thread(sg_client.send, mail)
        elif callable(sg_client):
          if inspect.iscoroutinefunction(sg_client):
            response = await sg_client(mail)
          else:
            response = await asyncio.to_thread(sg_client, mail)
        else:
          raise ValueError(f"Invalid SendGrid client: {sg_client}")

        # Check response status code
        status_code = getattr(response, "status_code", 200)
        if 200 <= status_code < 300:
          duration = (datetime.now(timezone.utc) - start_time).total_seconds()
          logger.info(
              "SendGrid email successfully sent to %s (status=%d, attempts=%d)",
              recipient_email,
              status_code,
              attempt_num,
          )
          result = {
              "status": "sent",
              "status_code": status_code,
              "recipient": recipient_email,
              "subject": subject,
              "attempts": attempt_num,
              "retries": attempt,
              "retry_history": retry_history,
              "duration_seconds": duration,
          }
          if metadata:
            result.update(metadata)
          return result
        else:
          # Non-2xx response returned without raising exception
          raise RuntimeError(f"Unexpected SendGrid response status: {status_code}")

      except Exception as e:
        is_retriable, status_code, retry_after = is_retryable_error(
            e, self.retry_config.retryable_status_codes
        )
        last_error_str = str(e)
        logger.warning(
            "SendGrid dispatch attempt %d/%d failed: %s (retryable=%s, status=%s)",
            attempt_num,
            max_retries + 1,
            last_error_str,
            is_retriable,
            status_code,
        )

        if attempt < max_retries and is_retriable:
          backoff_delay = calculate_backoff_delay(
              attempt=attempt,
              base_delay=self.retry_config.base_delay,
              backoff_factor=self.retry_config.backoff_factor,
              max_delay=self.retry_config.max_delay,
              jitter=self.retry_config.jitter,
              jitter_factor=self.retry_config.jitter_factor,
              retry_after=retry_after,
          )
          retry_history.append({
              "attempt": attempt_num,
              "status_code": status_code,
              "error": last_error_str,
              "retryable": True,
              "delay_seconds": backoff_delay,
              "timestamp": datetime.now(timezone.utc).isoformat(),
          })
          logger.info(
              "Backing off for %.2fs before retry %d/%d...",
              backoff_delay,
              attempt + 1,
              max_retries,
          )
          if inspect.iscoroutinefunction(self.sleep_fn):
            await self.sleep_fn(backoff_delay)
          else:
            res = self.sleep_fn(backoff_delay)
            if inspect.isawaitable(res):
              await res
        else:
          retry_history.append({
              "attempt": attempt_num,
              "status_code": status_code,
              "error": last_error_str,
              "retryable": is_retriable,
              "delay_seconds": 0.0,
              "timestamp": datetime.now(timezone.utc).isoformat(),
          })
          duration = (datetime.now(timezone.utc) - start_time).total_seconds()
          logger.error(
              "SendGrid dispatch failed permanently for %s after %d attempts: %s",
              recipient_email,
              attempt_num,
              last_error_str,
          )
          result = {
              "status": "error",
              "error": last_error_str,
              "recipient": recipient_email,
              "subject": subject,
              "status_code": status_code,
              "attempts": attempt_num,
              "retries": attempt,
              "retry_history": retry_history,
              "duration_seconds": duration,
          }
          if metadata:
            result.update(metadata)
          return result

    # Fallback return (should not normally be reached)
    return {
        "status": "error",
        "error": "Exhausted all retry attempts",
        "recipient": recipient_email,
        "subject": subject,
        "attempts": max_retries + 1,
        "retries": max_retries,
        "retry_history": retry_history,
    }


async def send_audit_report(
    recipient_email: str,
    quiz_date: str,
    total_questions: int,
    approved_count: int,
    failed_questions: list[dict],
    dry_run: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_delay: float = DEFAULT_MAX_DELAY,
    client: Optional[Any] = None,
    sleep_fn: Optional[Callable[[float], Any]] = None,
    api_key: Optional[str] = None,
) -> dict:
  """Send audit report via SendGrid email with exponential backoff retries.

  Args:
    recipient_email: Email address to send to
    quiz_date: Date of the quiz (e.g., "2026-08-22")
    total_questions: Total number of questions in the quiz
    approved_count: Number of approved questions
    failed_questions: List of failed question details
    dry_run: If True, don't actually send the email (for testing)
    max_retries: Maximum number of retry attempts (default: 5)
    base_delay: Initial retry backoff interval in seconds (default: 1.0)
    backoff_factor: Exponential multiplier (default: 2.0 -> 1s, 2s, 4s, 8s, 16s)
    max_delay: Maximum delay cap in seconds (default: 32.0)
    client: Optional pre-configured SendGrid client instance (for testing)
    sleep_fn: Optional custom sleep callable (for testing)
    api_key: Optional API key override

  Returns:
    Dictionary with send status, status_code, attempts, retries, and email metadata
  """
  html_content = _build_html_report(
      quiz_date, total_questions, approved_count, failed_questions
  )

  if dry_run:
    return {
        "status": "dry_run",
        "recipient": recipient_email,
        "quiz_date": quiz_date,
        "approved": approved_count,
        "total": total_questions,
        "failed_count": len(failed_questions),
        "attempts": 0,
        "retries": 0,
        "retry_history": [],
        "html_preview": html_content[:500] + "...",
    }

  mail = Mail(
      from_email=From("auditor@quizzy-news.internal"),
      to_emails=To(recipient_email),
      subject=f"Quizzy Auditor Report — {quiz_date}",
      html_content=Content("text/html", html_content),
  )

  retry_config = RetryConfig(
      max_retries=max_retries,
      base_delay=base_delay,
      backoff_factor=backoff_factor,
      max_delay=max_delay,
  )

  dispatcher = SendGridDispatcher(
      api_key=api_key,
      retry_config=retry_config,
      client=client,
      sleep_fn=sleep_fn,
  )

  metadata = {
      "quiz_date": quiz_date,
      "approved": approved_count,
      "total": total_questions,
      "failed_count": len(failed_questions),
  }

  return await dispatcher.dispatch_mail_with_retry(
      mail=mail,
      recipient_email=recipient_email,
      subject=f"Quizzy Auditor Report — {quiz_date}",
      metadata=metadata,
  )



async def send_no_quiz_alert(
    recipient_email: str,
    quiz_date: str,
    attempts: int,
    dry_run: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_delay: float = DEFAULT_MAX_DELAY,
    client: Optional[Any] = None,
    sleep_fn: Optional[Callable[[float], Any]] = None,
    api_key: Optional[str] = None,
) -> dict:
  """Send an alert when no quiz was available after repeated hourly checks.

  Args:
    recipient_email: Email address to send to
    quiz_date: Date the auditor was checking for (e.g., "2026-08-28")
    attempts: Number of hourly fetch attempts made before giving up
    dry_run: If True, don't actually send the email (for testing)
    max_retries: Maximum number of retry attempts (default: 5)
    base_delay: Initial retry backoff interval in seconds (default: 1.0)
    backoff_factor: Exponential multiplier (default: 2.0 -> 1s, 2s, 4s, 8s, 16s)
    max_delay: Maximum delay cap in seconds (default: 32.0)
    client: Optional pre-configured SendGrid client instance (for testing)
    sleep_fn: Optional custom sleep callable (for testing)
    api_key: Optional API key override

  Returns:
    Dictionary with send status, status_code, attempts, retries, and email metadata
  """
  html_content = _build_no_quiz_html(quiz_date, attempts)
  subject = f"Quizzy Auditor — No quiz found for {quiz_date}"

  if dry_run:
    return {
        "status": "dry_run",
        "recipient": recipient_email,
        "quiz_date": quiz_date,
        "fetch_attempts": attempts,
        "attempts": 0,
        "retries": 0,
        "retry_history": [],
        "html_preview": html_content[:500] + "...",
    }

  mail = Mail(
      from_email=From("auditor@quizzy-news.internal"),
      to_emails=To(recipient_email),
      subject=subject,
      html_content=Content("text/html", html_content),
  )

  retry_config = RetryConfig(
      max_retries=max_retries,
      base_delay=base_delay,
      backoff_factor=backoff_factor,
      max_delay=max_delay,
  )

  dispatcher = SendGridDispatcher(
      api_key=api_key,
      retry_config=retry_config,
      client=client,
      sleep_fn=sleep_fn,
  )

  metadata = {"quiz_date": quiz_date, "fetch_attempts": attempts}

  return await dispatcher.dispatch_mail_with_retry(
      mail=mail,
      recipient_email=recipient_email,
      subject=subject,
      metadata=metadata,
  )


def _build_no_quiz_html(quiz_date: str, attempts: int) -> str:
  """Build HTML email alert for when no quiz was available to audit."""
  return f"""
  <html>
    <head>
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ background: white; max-width: 600px; margin: 0 auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .header {{ padding: 24px; background: linear-gradient(135deg, #e95750 0%, #c23f39 100%); color: white; border-radius: 8px 8px 0 0; }}
        .content {{ padding: 24px; font-size: 14px; color: #333; line-height: 1.6; }}
        .footer {{ padding: 16px; border-top: 1px solid #eee; font-size: 12px; color: #666; text-align: center; }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1 style="margin: 0; font-size: 22px;">No Quiz Available</h1>
          <p style="margin: 8px 0 0 0; opacity: 0.9;">{quiz_date}</p>
        </div>
        <div class="content">
          <p>The auditor checked for today's quiz {attempts} times, roughly
          an hour apart, and never received any questions to review from
          quizzy-news-service.</p>
          <p>No audit was performed for {quiz_date}. Please check whether
          the quiz service published today's quiz.</p>
        </div>
        <div class="footer">
          <p>This is an automated alert from Quizzy Auditor running on Google ADK.</p>
        </div>
      </div>
    </body>
  </html>
  """


def _build_html_report(
    quiz_date: str, total: int, approved: int, failed_questions: list[dict]
) -> str:
  """Build HTML email report for the audit summary."""
  failed_count = total - approved
  html_rows = ""

  for q in failed_questions:
    html_rows += f"""
    <tr style="border-bottom: 1px solid #eee;">
      <td style="padding: 12px; text-align: left; font-size: 14px;">
        {q.get('question', 'N/A')[:100]}...
      </td>
      <td style="padding: 12px; text-align: left; font-size: 14px; color: #d32f2f;">
        {q.get('review', 'Failed')}
      </td>
    </tr>
    """

  return f"""
  <html>
    <head>
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ background: white; max-width: 600px; margin: 0 auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .header {{ padding: 24px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 8px 8px 0 0; }}
        .content {{ padding: 24px; }}
        .summary {{ background: #f9f9f9; padding: 16px; border-radius: 6px; margin-bottom: 20px; }}
        .stat {{ display: inline-block; margin-right: 20px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
        .passed {{ color: #4caf50; }}
        .failed {{ color: #d32f2f; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th {{ padding: 12px; text-align: left; background: #f5f5f5; font-weight: 600; font-size: 13px; color: #333; }}
        .footer {{ padding: 16px; border-top: 1px solid #eee; font-size: 12px; color: #666; text-align: center; }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1 style="margin: 0; font-size: 24px;">Quizzy Auditor Report</h1>
          <p style="margin: 8px 0 0 0; opacity: 0.9;">Daily Quality Audit — {quiz_date}</p>
        </div>
        <div class="content">
          <div class="summary">
            <div class="stat">
              <div class="stat-value passed">{approved}</div>
              <div class="stat-label">Approved</div>
            </div>
            <div class="stat">
              <div class="stat-value failed">{failed_count}</div>
              <div class="stat-label">Failed</div>
            </div>
            <div class="stat">
              <div class="stat-value">{total}</div>
              <div class="stat-label">Total Questions</div>
            </div>
          </div>

          {"" if not failed_questions else f"""
          <h2 style="font-size: 16px; margin-top: 24px; margin-bottom: 12px; color: #333;">Failed Questions</h2>
          <table>
            <thead>
              <tr>
                <th>Question</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {html_rows}
            </tbody>
          </table>
          """}
        </div>
        <div class="footer">
          <p>This is an automated report from Quizzy Auditor running on Google ADK.</p>
          <p>Report date: {quiz_date} | <a href="https://quizzy-auditor.example.com" style="color: #667eea;">View Full Dashboard</a></p>
        </div>
      </div>
    </body>
  </html>
  """
