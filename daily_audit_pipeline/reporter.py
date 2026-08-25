from datetime import datetime
from typing import AsyncGenerator
import os

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.cloud import firestore
from google.genai import types
from typing_extensions import override

from .sendgrid_dispatch import send_audit_report
from .schemas import QuestionAudit


class ReporterAgent(BaseAgent):
  """Tool-only agent: reads audit results from session state, writes to
  Firestore, and sends SendGrid notification if any questions failed.
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

    # Calculate summary stats
    total_questions = len(audit_results)
    approved_count = sum(1 for q in audit_results if q.approved)
    failed_questions = [
        {"question": q.question, "review": q.review if q.review else ""}
        for q in audit_results
        if not q.approved
    ]

    # Save report to Firestore (audits/{quizDate} per SPEC.md §4)
    questions_data = [
        {"question": q.question, "approved": q.approved, "review": q.review if q.review else ""}
        for q in audit_results
    ]

    report_doc = {
        "quizDate": quiz_date,
        "generatedAt": datetime.now().isoformat() + "Z",
        "model": "gemini-3.7-flash",
        "questions": questions_data,
        "summary": {
            "total": total_questions,
            "approved": approved_count,
            "failed": len(failed_questions),
        },
    }

    try:
      project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
      db = firestore.Client(project=project_id) if project_id else firestore.Client()
      # Set document using quiz_date and audit_quiz_date for compatibility
      db.collection("audits").document(quiz_date).set(report_doc)
      firestore_status = "saved"
    except Exception as e:
      print(f"Warning: Could not save report to Firestore: {e}")
      firestore_status = f"skipped ({e})"

    # Send email notification if there are failed questions
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

    yield Event(
        invocation_id=ctx.invocation_id,
        author=self.name,
        branch=ctx.branch,
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"Report Summary:\n"
                    f"- Date: {quiz_date}\n"
                    f"- Total: {total_questions}\n"
                    f"- Approved: {approved_count}\n"
                    f"- Failed: {len(failed_questions)}\n"
                    f"\n{status_text}"
                )
            ],
        ),
    )
