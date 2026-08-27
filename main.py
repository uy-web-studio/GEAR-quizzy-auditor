"""Cloud Run entry point for Quizzy Auditor.

This FastAPI application serves as the Cloud Run service that:
1. Receives authenticated requests from Cloud Scheduler
2. Runs the daily audit pipeline (FetcherAgent → AuditorAgent → ReporterAgent)
3. Provides dashboard endpoints for viewing audit history
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from google.auth.transport import requests
from google.cloud import firestore
from google.oauth2 import id_token
from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from daily_audit_pipeline.agent import root_agent

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
    badge = (
        '<span class="badge good">PASS</span>' if audit["failed"] == 0
        else '<span class="badge bad">FAIL</span>'
    )
    rows_html += f"""
    <tr>
      <td><a href="/reports/{audit['date']}">{audit['date']}</a></td>
      <td style="text-align: center;">{audit['total']}</td>
      <td style="text-align: center; color: var(--good);">{audit['approved']}</td>
      <td style="text-align: center; color: var(--bad);">{audit['failed']}</td>
      <td style="text-align: center;">{badge}</td>
    </tr>
    """

  passed_count = sum(1 for a in audit_list if a["failed"] == 0)
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
            <div class="stat-card good">
              <div class="stat-value">{approved}</div>
              <div class="stat-label">Approved</div>
            </div>
            <div class="stat-card bad">
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

  try:
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="quizzy_auditor", user_id="scheduler"
    )
    runner = Runner(
        agent=root_agent,
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
