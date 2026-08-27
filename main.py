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
    status = "✅ Pass" if audit["failed"] == 0 else "❌ Fail"
    rows_html += f"""
    <tr>
      <td><a href="/reports/{audit['date']}" style="color: #667eea; text-decoration: none;">{audit['date']}</a></td>
      <td style="text-align: center;">{audit['total']}</td>
      <td style="text-align: center; color: #4caf50;">{audit['approved']}</td>
      <td style="text-align: center; color: #d32f2f;">{audit['failed']}</td>
      <td style="text-align: center;">{status}</td>
    </tr>
    """

  return f"""
  <!DOCTYPE html>
  <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Quizzy Auditor Dashboard</title>
      <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto; background: #f5f5f5; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px 20px; text-align: center; }}
        .header h1 {{ font-size: 32px; margin-bottom: 8px; }}
        .header p {{ opacity: 0.9; font-size: 16px; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }}
        .stat-card {{ background: white; padding: 24px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .stat-value {{ font-size: 32px; font-weight: bold; color: #667eea; }}
        .stat-label {{ font-size: 14px; color: #666; text-transform: uppercase; margin-top: 8px; }}
        .table-container {{ background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); overflow: hidden; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ background: #f5f5f5; padding: 16px; text-align: left; font-weight: 600; font-size: 13px; color: #333; border-bottom: 1px solid #eee; }}
        td {{ padding: 16px; border-bottom: 1px solid #eee; }}
        tr:hover {{ background: #f9f9f9; }}
        .footer {{ text-align: center; margin-top: 40px; color: #666; font-size: 14px; }}
      </style>
    </head>
    <body>
      <div class="header">
        <h1>🎯 Quizzy Auditor</h1>
        <p>Daily News Quiz Quality Audit Dashboard</p>
      </div>
      <div class="container">
        <div class="stats">
          <div class="stat-card">
            <div class="stat-value">{len(audit_list)}</div>
            <div class="stat-label">Total Audits</div>
          </div>
          <div class="stat-card">
            <div class="stat-value" style="color: #4caf50;">{sum(1 for a in audit_list if a['failed'] == 0)}</div>
            <div class="stat-label">Passed</div>
          </div>
          <div class="stat-card">
            <div class="stat-value" style="color: #d32f2f;">{sum(1 for a in audit_list if a['failed'] > 0)}</div>
            <div class="stat-label">Failed</div>
          </div>
        </div>

        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th style="text-align: center;">Total</th>
                <th style="text-align: center;">Approved</th>
                <th style="text-align: center;">Failed</th>
                <th style="text-align: center;">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows_html if rows_html else '<tr><td colspan="5" style="text-align: center; color: #999; padding: 40px;">No audit reports yet</td></tr>'}
            </tbody>
          </table>
        </div>

        <div class="footer">
          <p>Quizzy Auditor — Running on Google ADK + Gemini 3.7 Flash</p>
          <p>Last update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
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

    failed_rows = ""
    for result in audit_results:
      if not result.get("approved", False):
        failed_rows += f"""
        <tr>
          <td style="padding: 12px;">{result.get('question', 'N/A')}</td>
          <td style="padding: 12px; color: #d32f2f;">{result.get('review', 'Failed')}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Audit Report — {date}</title>
        <style>
          * {{ margin: 0; padding: 0; box-sizing: border-box; }}
          body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto; background: #f5f5f5; }}
          .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px 20px; }}
          .container {{ max-width: 900px; margin: 0 auto; }}
          .back-link {{ color: rgba(255,255,255,0.8); text-decoration: none; margin-bottom: 16px; display: inline-block; }}
          .back-link:hover {{ color: white; }}
          .header h1 {{ font-size: 28px; margin-bottom: 8px; }}
          .content {{ background: white; margin: 20px; padding: 32px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
          .summary {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 32px; }}
          .summary-item {{ padding: 16px; background: #f9f9f9; border-radius: 6px; }}
          .summary-value {{ font-size: 24px; font-weight: bold; }}
          .summary-label {{ font-size: 12px; color: #666; text-transform: uppercase; margin-top: 8px; }}
          .failed-section {{ margin-top: 32px; }}
          .failed-section h2 {{ margin-bottom: 16px; }}
          table {{ width: 100%; border-collapse: collapse; }}
          th {{ background: #f5f5f5; padding: 12px; text-align: left; font-weight: 600; border-bottom: 1px solid #eee; }}
          td {{ padding: 12px; border-bottom: 1px solid #eee; }}
          .footer {{ text-align: center; padding: 20px; color: #666; font-size: 13px; }}
        </style>
      </head>
      <body>
        <div class="header">
          <div class="container">
            <a href="/" class="back-link">← Back to Dashboard</a>
            <h1>Audit Report for {date}</h1>
          </div>
        </div>
        <div class="container">
          <div class="content">
            <div class="summary">
              <div class="summary-item">
                <div class="summary-value">{total}</div>
                <div class="summary-label">Total Questions</div>
              </div>
              <div class="summary-item">
                <div class="summary-value" style="color: #4caf50;">{approved}</div>
                <div class="summary-label">Approved</div>
              </div>
              <div class="summary-item">
                <div class="summary-value" style="color: #d32f2f;">{failed}</div>
                <div class="summary-label">Failed</div>
              </div>
            </div>

            {"" if not failed_rows else f'''
            <div class="failed-section">
              <h2>Failed Questions</h2>
              <table>
                <thead>
                  <tr>
                    <th>Question</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {failed_rows}
                </tbody>
              </table>
            </div>
            '''}

            <div class="footer">
              <p>Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
            </div>
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
