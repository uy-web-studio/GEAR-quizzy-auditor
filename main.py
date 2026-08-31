"""Cloud Run entry point for Quizzy Auditor.

This FastAPI application serves as the Cloud Run service that:
1. Receives authenticated requests from Cloud Scheduler
2. Runs the daily audit pipeline (FetcherAgent → AuditorAgent → ReporterAgent)
3. Provides dashboard endpoints for viewing audit history
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from google.auth.transport import requests
from google.cloud import firestore
from google.oauth2 import id_token
from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from pydantic import BaseModel

import auth
from daily_audit_pipeline.agent import DEFAULT_AUDITOR_INSTRUCTION, build_daily_pipeline
from daily_audit_pipeline.revisions import log_revision

app = FastAPI(
    title="Quizzy Auditor",
    description="Daily news quiz quality audit and reporting system",
)

# Visual language borrowed from quizzy.news (the game this pipeline audits):
# soft sky-blue field, Lexend, and flat/solid "pressed" shadows — never
# blurred — colored a darker shade of whatever they sit under.
FONT_LINKS = """\
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Lexend:wght@400;600;700&display=swap" rel="stylesheet">"""

SHARED_STYLES = """\
    :root {
      --bg: #eaf4fe;
      --card: #ffffff;
      --ink: #3d3d3d;
      --muted: #909090;
      --line: #e3e3e3;
      --brand: #53adf0;
      --brand-shadow: #3a8ac9;
      --good: #6ba530;
      --good-shadow: #548024;
      --good-tint: #defebf;
      --bad: #e95750;
      --bad-shadow: #c23f39;
      --bad-tint: #fad1d1;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Lexend', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    a { color: var(--brand); }
    a:hover { text-decoration: underline; }
    a:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; border-radius: 4px; }
    .container { max-width: 1080px; margin: 0 auto; padding: 0 20px 40px; }
    .topbar { padding: 40px 20px 28px; text-align: center; }
    .topbar h1 {
      display: inline-block;
      font-size: 30px;
      font-weight: 700;
      letter-spacing: 0.01em;
      padding-bottom: 8px;
      border-bottom: 3px dashed var(--brand);
    }
    .topbar p { margin-top: 14px; color: var(--muted); font-size: 15px; }
    .page-header { padding: 32px 20px 8px; }
    .page-header h1 { font-size: 24px; font-weight: 700; margin-top: 10px; }
    .back-link { color: var(--brand); text-decoration: none; font-weight: 600; font-size: 14px; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; margin: 8px 0 28px; }
    .stat-card { background: var(--card); border-radius: 10px; padding: 20px 22px; box-shadow: 0 4px 0 #c7c7c7; }
    .stat-card.good { box-shadow: 0 4px 0 var(--good-shadow); }
    .stat-card.bad { box-shadow: 0 4px 0 var(--bad-shadow); }
    .stat-value { font-size: 30px; font-weight: 700; }
    .stat-card.good .stat-value { color: var(--good); }
    .stat-card.bad .stat-value { color: var(--bad); }
    .stat-label { margin-top: 6px; font-size: 12px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
    .table-wrap { background: var(--card); border-radius: 10px; box-shadow: 0 4px 0 #c7c7c7; overflow: hidden; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 480px; }
    th { text-align: left; font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); padding: 14px 18px; border-bottom: 1px solid var(--line); }
    td { padding: 14px 18px; border-bottom: 1px solid var(--line); font-size: 14px; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f5faff; }
    .badge { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .badge.good { background: var(--good-tint); color: var(--good-shadow); }
    .badge.bad { background: var(--bad-tint); color: var(--bad-shadow); }
    .badge.neutral { background: #ececec; color: var(--muted); }
    .card-list { display: flex; flex-direction: column; gap: 14px; margin-top: 16px; }
    .fail-card { background: var(--bad-tint); border-radius: 10px; padding: 18px 20px; box-shadow: 0 4px 0 var(--bad-shadow); }
    .fail-card .q { font-weight: 600; margin-bottom: 6px; }
    .fail-card .why { font-size: 13px; color: var(--bad-shadow); }
    .empty { text-align: center; color: var(--muted); padding: 48px 20px; }
    .footer { text-align: center; margin-top: 36px; color: var(--muted); font-size: 13px; }"""


def get_db():
  """Lazy initialization of Firestore client."""
  project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
  return firestore.Client(project=project_id) if project_id else firestore.Client()


def get_auditor_instruction(db) -> str:
  """Read the currently saved auditor rubric, falling back to the default."""
  doc = db.collection("config").document("auditor_rules").get()
  if doc.exists:
    data = doc.to_dict() or {}
    instruction = data.get("instruction")
    if instruction:
      return instruction
  return DEFAULT_AUDITOR_INSTRUCTION


def _render_admin_page(instruction: str, dry_run_result: Optional[dict], saved: bool) -> str:
  saved_banner = (
      '<div class="empty" style="color: var(--good-shadow); background: var(--good-tint); border-radius: 10px; padding: 12px;">Rules saved.</div>'
      if saved else ""
  )

  dry_run_html = ""
  if dry_run_result is not None:
    if dry_run_result.get("error"):
      dry_run_html = f'<div class="empty" style="color: var(--bad-shadow); background: var(--bad-tint); border-radius: 10px; padding: 12px;">Dry run failed: {dry_run_result["error"]}</div>'
    else:
      cards = "".join(
          f"""
          <div class="fail-card" style="background: var(--card); box-shadow: 0 4px 0 {'var(--good-shadow)' if q['approved'] else 'var(--bad-shadow)'};">
            <div class="q">{q['question']} — {'PASS' if q['approved'] else 'FAIL'}</div>
            <div class="why">{q['review']}</div>
          </div>
          """
          for q in dry_run_result["questions"]
      )
      dry_run_html = f"""
      <h2 style="margin-top: 24px;">Dry Run Results ({dry_run_result['approved']}/{dry_run_result['total']} approved)</h2>
      <div class="card-list">{cards}</div>
      """

  return f"""
  <!DOCTYPE html>
  <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Quizzy Auditor — Edit Rules</title>
{FONT_LINKS}
      <style>
{SHARED_STYLES}
        textarea {{ width: 100%; min-height: 320px; font-family: monospace; font-size: 13px; padding: 12px; border-radius: 10px; border: 1px solid var(--line); box-sizing: border-box; }}
        .btn {{ display: inline-block; padding: 10px 20px; border-radius: 8px; border: none; background: var(--brand); color: white; font-weight: 700; font-size: 14px; cursor: pointer; box-shadow: 0 4px 0 var(--brand-shadow); }}
      </style>
    </head>
    <body>
      <div class="page-header container">
        <a href="/" class="back-link">← All audits</a>
        <h1>Edit Auditor Rules</h1>
      </div>
      <div class="container">
        {saved_banner}
        <form method="POST" action="/admin/rules">
          <textarea name="instruction">{instruction}</textarea>
          <div style="margin-top: 12px;">
            <button class="btn" type="submit" name="action" value="save">Save</button>
            <button class="btn" type="submit" name="action" value="dry_run" style="background: var(--good); box-shadow: 0 4px 0 var(--good-shadow);">Dry Run (today's live quiz)</button>
          </div>
          <input type="hidden" name="target" value="today">
        </form>
        {dry_run_html}
      </div>
    </body>
  </html>
  """


@app.get("/admin", response_class=HTMLResponse)
async def admin_rules_page(admin_email: str = Depends(auth.require_admin)):
  """Rule editor — auth-gated. Shows the current saved rubric in a textarea."""
  db = get_db()
  instruction = get_auditor_instruction(db)
  return _render_admin_page(instruction=instruction, dry_run_result=None, saved=False)


@app.post("/admin/rules", response_class=HTMLResponse)
async def admin_rules_action(
    instruction: str = Form(...),
    action: str = Form(...),
    target: str = Form("today"),
    admin_email: str = Depends(auth.require_admin),
):
  """Save an edited rubric instruction (action=save)."""
  db = get_db()

  # action == "dry_run" is handled in Task 10; "save" is this task's scope.
  before = get_auditor_instruction(db)
  db.collection("config").document("auditor_rules").set({
      "instruction": instruction,
      "updatedAt": datetime.now().isoformat() + "Z",
      "updatedBy": admin_email,
  })
  log_revision(
      db,
      revision_type="rule_change",
      actor=admin_email,
      target={"scope": "auditor_rules"},
      before={"instruction": before},
      after={"instruction": instruction},
  )
  return _render_admin_page(instruction=instruction, dry_run_result=None, saved=True)


def verify_cloud_scheduler_request(request: Request) -> bool:
  """Verify that the request came from Cloud Scheduler via OIDC token.

  In production, Cloud Scheduler sends an Authorization header with an
  OIDC token. For local testing, set SKIP_AUTH=true.
  """
  if os.environ.get("SKIP_AUTH", "").lower() in ("true", "1", "yes"):
    return True

  try:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
      return False

    token = auth_header[7:]
    # Verify the token's signature and claims
    # In production, this validates the token came from Cloud Scheduler's service account
    id_token.verify_oauth2_token(
        token, requests.Request(), audience=os.environ.get("SERVICE_URL")
    )
    return True
  except Exception as e:
    print(f"Token verification failed: {e}")
    return False


@app.get("/", response_class=HTMLResponse)
async def dashboard():
  """Render the audit dashboard homepage."""
  audit_list = []
  try:
    db = get_db()
    audits = db.collection("audits").order_by("quizDate", direction="DESCENDING").limit(30).stream()
    seen_dates = set()
    for doc in audits:
      data = doc.to_dict()
      date = data.get("quizDate") or data.get("quiz_date", "unknown")
      if date in seen_dates:
        continue
      seen_dates.add(date)
      total = data.get("total_questions", data.get("summary", {}).get("total", 0))
      approved = data.get("approved", data.get("summary", {}).get("approved", 0))
      failed = data.get("failed", data.get("summary", {}).get("failed", max(0, total - approved)))
      audit_list.append({
          "date": date,
          "total": total,
          "approved": approved,
          "failed": failed,
      })
  except Exception as e:
    print(f"Error fetching audits: {e}")
    audit_list = []

  rows_html = ""
  for audit in audit_list:
    if audit["total"] == 0:
      badge = '<span class="badge neutral">NO QUIZ</span>'
    elif audit["failed"] == 0:
      badge = '<span class="badge good">PASS</span>'
    else:
      badge = '<span class="badge bad">FAIL</span>'
    rows_html += f"""
    <tr>
      <td><a href="/reports/{audit['date']}">{audit['date']}</a></td>
      <td style="text-align: center;">{audit['total']}</td>
      <td style="text-align: center; color: var(--good);">{audit['approved']}</td>
      <td style="text-align: center; color: var(--bad);">{audit['failed']}</td>
      <td style="text-align: center;">{badge}</td>
    </tr>
    """

  no_quiz_count = sum(1 for a in audit_list if a["total"] == 0)
  passed_count = sum(1 for a in audit_list if a["total"] > 0 and a["failed"] == 0)
  failed_count = sum(1 for a in audit_list if a["failed"] > 0)
  empty_state = (
      '<div class="empty">No audits yet — the first scheduled run '
      "(daily, 9am America/Los_Angeles) will populate this page "
      "automatically.</div>"
  )

  return f"""
  <!DOCTYPE html>
  <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Quizzy Auditor Dashboard</title>
{FONT_LINKS}
      <style>
{SHARED_STYLES}
      </style>
    </head>
    <body>
      <div class="topbar">
        <h1>Quizzy Auditor</h1>
        <p>Daily QC for quizzy.news — {len(audit_list)} audit{"" if len(audit_list) == 1 else "s"} recorded</p>
      </div>
      <div class="container">
        <div class="stats">
          <div class="stat-card">
            <div class="stat-value">{len(audit_list)}</div>
            <div class="stat-label">Total Audits</div>
          </div>
          <div class="stat-card good">
            <div class="stat-value">{passed_count}</div>
            <div class="stat-label">Passed</div>
          </div>
          <div class="stat-card bad">
            <div class="stat-value">{failed_count}</div>
            <div class="stat-label">Failed</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{no_quiz_count}</div>
            <div class="stat-label">No Quiz</div>
          </div>
        </div>

        {'<div class="table-wrap"><table><thead><tr><th>Date</th><th style="text-align: center;">Total</th><th style="text-align: center;">Approved</th><th style="text-align: center;">Failed</th><th style="text-align: center;">Result</th></tr></thead><tbody>' + rows_html + '</tbody></table></div>' if rows_html else empty_state}

        <div class="footer">
          <p>Quizzy Auditor — Google ADK + Gemini 3.7 Flash</p>
          <p>Last checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
        </div>
      </div>
    </body>
  </html>
  """


class LoginRequest(BaseModel):
  credential: str


@app.post("/admin/login")
async def admin_login(body: LoginRequest):
  """Verify a Google Identity Services ID token and issue a session cookie."""
  client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
  try:
    email = auth.verify_google_id_token(body.credential, client_id=client_id)
  except Exception as e:
    raise HTTPException(status_code=401, detail=str(e))

  cookie_value = auth.create_session_cookie(email)
  response = JSONResponse({"status": "ok", "email": email})
  response.set_cookie(
      key=auth.SESSION_COOKIE_NAME,
      value=cookie_value,
      httponly=True,
      secure=True,
      samesite="strict",
      max_age=auth.SESSION_TTL_SECONDS,
  )
  return response


@app.post("/admin/logout")
async def admin_logout():
  """Clear the admin session cookie."""
  response = JSONResponse({"status": "ok"})
  response.delete_cookie(auth.SESSION_COOKIE_NAME)
  return response


@app.get("/reports/{date}", response_class=HTMLResponse)
async def report_detail(date: str):
  """Render detailed audit report for a specific date."""
  try:
    db = get_db()
    doc = db.collection("audits").document(date).get()
    if not doc.exists:
      raise HTTPException(status_code=404, detail="Report not found")

    data = doc.to_dict()
    total = data.get("total_questions", data.get("summary", {}).get("total", 0))
    approved = data.get("approved", data.get("summary", {}).get("approved", 0))
    failed = data.get("failed", data.get("summary", {}).get("failed", max(0, total - approved)))
    audit_results = data.get("audit_results") or data.get("questions", [])
    status = data.get("status")
    fetch_attempts = data.get("fetchAttempts")

    if total == 0:
      # No quiz was available for the auditor to review — distinct from
      # "audited N questions and none failed". See ReporterAgent's no-quiz
      # retry policy (checks hourly, alerts after MAX_FETCH_ATTEMPTS).
      if status == "empty_final" or (fetch_attempts and fetch_attempts >= 4):
        failed_section = (
            '<div class="empty">No quiz was available to audit — the '
            f'fetcher checked {fetch_attempts or "several"} times and never '
            "received questions. The team has been alerted.</div>"
        )
      else:
        retry_note = f" (attempt {fetch_attempts}/4, retrying hourly)" if fetch_attempts else ""
        failed_section = (
            f'<div class="empty">No quiz was available to audit yet{retry_note} '
            "— nothing to review.</div>"
        )
      stat_approved_class = ""
      stat_failed_class = ""
    else:
      failed_questions = [r for r in audit_results if not r.get("approved", False)]
      fail_cards = "".join(
          f"""
          <div class="fail-card">
            <div class="q">{r.get('question', 'N/A')}</div>
            <div class="why">{r.get('review') or 'Failed'}</div>
          </div>
          """
          for r in failed_questions
      )
      failed_section = (
          f'<h2 style="margin-top: 32px; margin-bottom: 4px;">Failed Questions</h2>'
          f'<div class="card-list">{fail_cards}</div>'
          if fail_cards
          else '<div class="empty">All questions passed — nothing to review.</div>'
      )
      stat_approved_class = "good"
      stat_failed_class = "bad"

    return f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Audit Report — {date}</title>
{FONT_LINKS}
        <style>
{SHARED_STYLES}
        </style>
      </head>
      <body>
        <div class="page-header container">
          <a href="/" class="back-link">← All audits</a>
          <h1>Audit — {date}</h1>
        </div>
        <div class="container">
          <div class="stats">
            <div class="stat-card">
              <div class="stat-value">{total}</div>
              <div class="stat-label">Total Questions</div>
            </div>
            <div class="stat-card {stat_approved_class}">
              <div class="stat-value">{approved}</div>
              <div class="stat-label">Approved</div>
            </div>
            <div class="stat-card {stat_failed_class}">
              <div class="stat-value">{failed}</div>
              <div class="stat-label">Failed</div>
            </div>
          </div>

          {failed_section}

          <div class="footer">
            <p>Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
          </div>
        </div>
      </body>
    </html>
    """
  except HTTPException:
    raise
  except Exception as e:
    print(f"Error rendering report: {e}")
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/trigger-audit")
async def trigger_audit(request: Request):
  """Trigger the daily audit pipeline (called by Cloud Scheduler).

  Cloud Scheduler sends an authenticated OIDC request to this endpoint.
  """
  # Verify the request is from Cloud Scheduler
  if not verify_cloud_scheduler_request(request):
    raise HTTPException(status_code=401, detail="Unauthorized")

  # The scheduler fires hourly so ReporterAgent can retry a no-quiz day up
  # to MAX_FETCH_ATTEMPTS times before alerting. Once today's audit is
  # resolved (real questions saved, or the retry budget is exhausted and
  # the alert sent), skip re-running the pipeline for the rest of the day.
  quiz_date_today = datetime.now().strftime("%Y-%m-%d")
  db = get_db()
  try:
    existing_doc = db.collection("audits").document(quiz_date_today).get()
    if existing_doc.exists:
      existing_status = (existing_doc.to_dict() or {}).get("status")
      if existing_status in ("complete", "empty_final"):
        return {
            "status": "skipped",
            "timestamp": datetime.now().isoformat(),
            "message": (
                f"Audit for {quiz_date_today} already resolved "
                f"(status={existing_status}); not re-running."
            ),
        }
  except Exception as e:
    print(f"Warning: could not check existing audit status before trigger: {e}")

  try:
    instruction = get_auditor_instruction(db)
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="quizzy_auditor", user_id="scheduler"
    )
    runner = Runner(
        agent=build_daily_pipeline(instruction),
        app_name="quizzy_auditor",
        session_service=session_service,
    )
    events = []
    async for event in runner.run_async(
        user_id="scheduler",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text="Run daily quiz audit")],
        ),
    ):
      events.append(event)

    return {
        "status": "completed",
        "timestamp": datetime.now().isoformat(),
        "events_count": len(events),
        "message": "Daily audit pipeline executed successfully",
    }
  except Exception as e:
    print(f"Error triggering audit: {e}")
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
  """Health check endpoint for Cloud Run."""
  return {"status": "healthy"}


if __name__ == "__main__":
  import uvicorn

  port = int(os.environ.get("PORT", 8080))
  uvicorn.run(app, host="0.0.0.0", port=port)
