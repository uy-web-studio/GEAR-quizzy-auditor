from datetime import datetime
from typing import AsyncGenerator
import os

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.cloud import firestore
from google.genai import types
from typing_extensions import override

from .grounding import cross_validate_sources, extract_grounding_activity
from .sendgrid_dispatch import send_audit_report, send_no_quiz_alert
from .schemas import QuestionAudit

# How many hourly checks the scheduler will make for a day's quiz before
# ReporterAgent gives up and alerts, per SPEC.md's no-quiz retry policy.
MAX_FETCH_ATTEMPTS = 4


def save_audit_report(db, quiz_date: str, report_doc: dict) -> str:
  """Write a report doc to audits/{quiz_date}.

  Module-level (not a ReporterAgent method) so Phase 2's retroactive
  re-audit endpoint can reuse the exact same save path.
  """
  if db is None:
    return "skipped (no Firestore client)"
  try:
    db.collection("audits").document(quiz_date).set(report_doc)
    return "saved"
  except Exception as e:
    print(f"Warning: Could not save report to Firestore: {e}")
    return f"skipped ({e})"


class ReporterAgent(BaseAgent):
  """Tool-only agent: reads audit results from session state, writes to
  Firestore, and sends SendGrid notification if any questions failed — or,
  once no quiz has been available for MAX_FETCH_ATTEMPTS consecutive hourly
  checks, sends a no-quiz alert instead.
  """

  @override
  async def _run_async_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    # Extract audit results from session state
    audit_results: list[QuestionAudit] = []
    quiz_date = datetime.now().strftime("%Y-%m-%d")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", "donovanuy@gmail.com")

    # Get the session state to extract audit_results
    session = ctx.session
    if session and session.state:
      audit_results_raw = session.state.get("audit_results", [])
      audit_results = [
          QuestionAudit(**r) if isinstance(r, dict) else r
          for r in audit_results_raw
      ]

    total_questions = len(audit_results)
    grounding_activity = extract_grounding_activity(session.events if session else [])

    db = None
    try:
      project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
      db = firestore.Client(project=project_id) if project_id else firestore.Client()
    except Exception as e:
      print(f"Warning: Could not initialize Firestore client: {e}")

    if total_questions > 0:
      summary_text = await self._report_audited(
          db, quiz_date, recipient_email, audit_results, grounding_activity
      )
    else:
      summary_text = await self._report_no_quiz(db, quiz_date, recipient_email)

    yield Event(
        invocation_id=ctx.invocation_id,
        author=self.name,
        branch=ctx.branch,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=summary_text)],
        ),
    )

  async def _report_audited(
      self, db, quiz_date: str, recipient_email: str, audit_results, grounding_activity: dict
  ) -> str:
    """Save a normal report and email only if any question failed."""
    total_questions = len(audit_results)
    approved_count = sum(1 for q in audit_results if q.approved)
    failed_questions = [
        {"question": q.question, "review": q.review if q.review else ""}
        for q in audit_results
        if not q.approved
    ]
    questions_data = [
        {
            "question": q.question,
            "choices": q.choices,
            "answer_matches_choice": q.answer_matches_choice,
            "approved": q.approved,
            "review": q.review if q.review else "",
            "sources_checked": cross_validate_sources(q.sources_checked, grounding_activity),
        }
        for q in audit_results
    ]

    report_doc = {
        "quizDate": quiz_date,
        "generatedAt": datetime.now().isoformat() + "Z",
        "model": "gemini-3.7-flash",
        "status": "complete",
        "questions": questions_data,
        "groundingActivity": grounding_activity,
        "summary": {
            "total": total_questions,
            "approved": approved_count,
            "failed": len(failed_questions),
        },
    }
    firestore_status = save_audit_report(db, quiz_date, report_doc)

    if failed_questions:
      dry_run = os.environ.get("SENDGRID_DRY_RUN", "false").lower() in ("true", "1", "yes")
      send_result = await send_audit_report(
          recipient_email=recipient_email,
          quiz_date=quiz_date,
          total_questions=total_questions,
          approved_count=approved_count,
          failed_questions=failed_questions,
          dry_run=dry_run,
      )
      status_text = f"Report {firestore_status}. SendGrid status: {send_result.get('status', 'unknown')}"
    else:
      status_text = f"Report {firestore_status}. All questions approved — no email sent."

    return (
        f"Report Summary:\n"
        f"- Date: {quiz_date}\n"
        f"- Total: {total_questions}\n"
        f"- Approved: {approved_count}\n"
        f"- Failed: {len(failed_questions)}\n"
        f"\n{status_text}"
    )

  async def _report_no_quiz(self, db, quiz_date: str, recipient_email: str) -> str:
    """Record a no-quiz check; only alert once MAX_FETCH_ATTEMPTS is reached.

    The scheduler re-triggers this pipeline hourly, and this method reads
    how many previous checks already came back empty today, so it can
    decide whether to wait for another hourly retry or give up and email.
    """
    previous_attempts = 0
    if db is not None:
      try:
        existing = db.collection("audits").document(quiz_date).get()
        if existing.exists:
          previous_attempts = (existing.to_dict() or {}).get("fetchAttempts", 0) or 0
      except Exception as e:
        print(f"Warning: Could not read existing audit doc: {e}")

    attempts = previous_attempts + 1
    final_attempt = attempts >= MAX_FETCH_ATTEMPTS

    report_doc = {
        "quizDate": quiz_date,
        "generatedAt": datetime.now().isoformat() + "Z",
        "model": "gemini-3.7-flash",
        "status": "empty_final" if final_attempt else "empty",
        "fetchAttempts": attempts,
        "questions": [],
        "summary": {"total": 0, "approved": 0, "failed": 0},
    }
    firestore_status = save_audit_report(db, quiz_date, report_doc)

    if final_attempt:
      dry_run = os.environ.get("SENDGRID_DRY_RUN", "false").lower() in ("true", "1", "yes")
      send_result = await send_no_quiz_alert(
          recipient_email=recipient_email,
          quiz_date=quiz_date,
          attempts=attempts,
          dry_run=dry_run,
      )
      status_text = (
          f"Report {firestore_status}. No quiz was available after {attempts} "
          f"attempts — alert sent (SendGrid status: {send_result.get('status', 'unknown')})."
      )
    else:
      status_text = (
          f"Report {firestore_status}. No quiz was available (attempt "
          f"{attempts}/{MAX_FETCH_ATTEMPTS}) — will retry next hour, no email sent yet."
      )

    return (
        f"Report Summary:\n"
        f"- Date: {quiz_date}\n"
        f"- Total: 0\n"
        f"- Fetch attempts: {attempts}/{MAX_FETCH_ATTEMPTS}\n"
        f"\n{status_text}"
    )
