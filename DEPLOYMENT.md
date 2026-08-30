# Quizzy Auditor Deployment Guide

This document describes how to deploy the Quizzy Auditor service to Google Cloud Run.

## Components Implemented

### 1. **Requirements & Dependencies** (`requirements.txt`)
- All necessary Python packages for the audit pipeline
- Includes: `google-adk`, `google-genai`, `sendgrid`, `google-cloud-firestore`, `google-cloud-secret-manager`, `fastapi`, `uvicorn`, and testing tools

### 2. **SendGrid Integration** (`daily_audit_pipeline/sendgrid_dispatch.py`)
- **`get_sendgrid_api_key()`**: Retrieves SendGrid API key from Secret Manager with fallback to env var
- **`send_audit_report()`**: Sends HTML-formatted audit summary email when questions fail
- **`_build_html_report()`**: Generates professional HTML email template with audit statistics

### 3. **ReporterAgent** (`daily_audit_pipeline/reporter.py`)
- Tool-only BaseAgent that reads audit results from session state
- Persists audit reports to Firestore under `audits/{quiz_date}`
- Triggers SendGrid email dispatch only when questions fail
- Generates audit summary: total, approved, failed counts

### 4. **Cloud Run Entry Point** (`main.py`)
- FastAPI application with three main endpoints:
  - `GET /` - Dashboard showing audit history with statistics
  - `GET /reports/{date}` - Detailed report for a specific date
  - `POST /trigger-audit` - Trigger the audit pipeline (called by Cloud Scheduler)
  - `GET /health` - Health check for Cloud Run

### 5. **Test Suite** (`tests/test_evaluator.py`)
- 15 comprehensive tests covering:
  - QuestionAudit schema validation
  - All four audit rubric rules (phrasing, answer/choice match, choice count, fact-check)
  - Complete audit flow scenarios (all approved, mixed, all failed)
- **Run tests**: `.venv/bin/python -m pytest tests/test_evaluator.py -v`

### 6. **SendGrid Test Script** (`scripts/test_sendgrid_dispatch.py`)
- **Dry-run mode** (default): Simulates email sending without requiring API key
- **Send mode**: Actually sends test email (requires SendGrid API key)
- **Usage**:
  ```bash
  python scripts/test_sendgrid_dispatch.py --dry-run  # Test without sending
  python scripts/test_sendgrid_dispatch.py --send     # Send actual test email
  ```

### 7. **Containerization** (`Dockerfile`)
- Python 3.12 slim base image
- Installs dependencies from `requirements.txt`
- Includes health check
- Exposes port 8080

### 8. **Cloud Build Config** (`cloudbuild.yaml`)
- 4-step build pipeline:
  1. Build Docker image
  2. Push to Container Registry
  3. Deploy via gke-deploy
  4. Deploy to Cloud Run with env vars

### 9. **Deployment Script** (`deploy.sh`)
- Automated deployment to Cloud Run
- Sets up IAM permissions for compute service account
- Creates/updates Cloud Scheduler job
- Configures environment variables via `gcloud run services update`
- Validates SendGrid secret in Secret Manager

## Pre-Deployment Checklist

### 1. GCP Project Setup
```bash
# Set your project ID
export PROJECT_ID="quizzy-auditor-hackathon"
gcloud config set project $PROJECT_ID
```

### 2. Enable Required APIs
```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    firestore.googleapis.com \
    cloudscheduler.googleapis.com \
    aiplatform.googleapis.com
```

### 3. Create Firestore Database
```bash
# Create Firestore in Native mode if not already created
gcloud firestore databases create --region=us-central1
```

### 4. Set Up SendGrid Secret
```bash
# Store your SendGrid API key in Secret Manager
echo "YOUR_SENDGRID_API_KEY" | \
    gcloud secrets create sendgrid-api-key \
    --replication-policy=automatic \
    --data-file=-
```

### 5. Configure Environment Variables
The deployment script sets these automatically, but for manual setup:

```bash
# After deploying to Cloud Run:
gcloud run services update quizzy-auditor \
    --update-env-vars=\
GOOGLE_CLOUD_PROJECT=quizzy-auditor-hackathon,\
GOOGLE_CLOUD_LOCATION=global,\
GOOGLE_GENAI_USE_ENTERPRISE=1,\
RECIPIENT_EMAIL=donovanuy@gmail.com
```

`FROM_EMAIL` is optional — defaults to `auditor@uyweb.studio` (a
domain-authenticated SendGrid sender, confirmed working 2026-08-29; see
SPEC.md §6 gotcha 6). Only set it to override with a different verified
sender identity.

## Deployment Steps

### Option A: Automated Deployment (Recommended)
```bash
cd /home/dceu/GEAR/quizzy-auditor
./deploy.sh
```

### Option B: Manual Deployment

**Step 1: Build and Push Image**
```bash
PROJECT_ID="quizzy-auditor-hackathon"
gcloud builds submit . --config=cloudbuild.yaml
```

**Step 2: Deploy to Cloud Run**
```bash
gcloud run deploy quizzy-auditor \
    --image=gcr.io/${PROJECT_ID}/quizzy-auditor:latest \
    --platform=managed \
    --region=us-central1 \
    --allow-unauthenticated \
    --memory=2Gi \
    --cpu=2 \
    --timeout=3600
```

**Step 3: Get Service URL and Grant Permissions**
```bash
SERVICE_URL=$(gcloud run services describe quizzy-auditor \
    --region=us-central1 --format='value(status.url)')

PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID \
    --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Grant required IAM roles
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/aiplatform.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/datastore.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/secretmanager.secretAccessor"
```

**Step 4: Set Environment Variables**
```bash
gcloud run services update quizzy-auditor \
    --update-env-vars=\
GOOGLE_CLOUD_PROJECT=${PROJECT_ID},\
GOOGLE_CLOUD_LOCATION=global,\
GOOGLE_GENAI_USE_ENTERPRISE=1,\
RECIPIENT_EMAIL=donovanuy@gmail.com
```

**Step 5: Create Cloud Scheduler Job**
```bash
gcloud scheduler jobs create http quizzy-auditor-daily-trigger \
    --location=us-central1 \
    --schedule="0 09 * * *" \
    --time-zone="America/Los_Angeles" \
    --uri="${SERVICE_URL}/trigger-audit" \
    --http-method=POST \
    --oidc-service-account-email="${COMPUTE_SA}" \
    --oidc-token-audience="${SERVICE_URL}"
```

## Testing

### Run Locally
```bash
# Install dependencies in venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run tests
.venv/bin/python -m pytest tests/test_evaluator.py -v

# Test SendGrid dispatch
GOOGLE_CLOUD_PROJECT=quizzy-auditor-hackathon \
    .venv/bin/python scripts/test_sendgrid_dispatch.py --dry-run
```

### Test Cloud Run Deployment
```bash
# Health check
curl ${SERVICE_URL}/health

# View dashboard
open ${SERVICE_URL}

# Manually trigger audit (requires a valid OIDC identity token — the
# deployed service does NOT accept a SKIP_AUTH query param; that env var
# is for local-only testing, see "Run Locally" above)
curl -X POST "${SERVICE_URL}/trigger-audit" \
    -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=${SERVICE_URL})"

# View logs
gcloud run logs read quizzy-auditor --region=us-central1 --limit=50
```

## Monitoring

### View Recent Audits
```bash
firestore_db=$(gcloud firestore databases list --format="value(name)")
gcloud firestore documents list --database="${firestore_db}" --collection-id="audits"
```

### Check Cloud Scheduler Job Status
```bash
# Describe the scheduled job
gcloud scheduler jobs describe quizzy-auditor-daily-trigger --location=us-central1

# View execution history
gcloud logging read "resource.type=cloud_scheduler_job" --limit=10 --format=json
```

### View Application Logs
```bash
# Stream recent logs
gcloud run logs read quizzy-auditor --region=us-central1 --follow

# Search for errors
gcloud logging read "resource.type=cloud_run_managed AND resource.labels.service_name=quizzy-auditor AND severity>=ERROR" --limit=50
```

## Troubleshooting

### SendGrid Emails Not Sending
- Check that SendGrid API key is correctly stored in Secret Manager
- Verify the recipient email is valid
- Check Cloud Run logs for SendGrid errors

### Firestore Connection Issues
- Ensure Firestore database is created and in "Native" mode
- Verify compute service account has `roles/datastore.user` permission
- Check that `GOOGLE_CLOUD_PROJECT` environment variable is set

### Gemini API Errors
- Verify compute service account has `roles/aiplatform.user` permission
- Ensure `GOOGLE_CLOUD_LOCATION=global` is set in environment
- Check that Gemini API is enabled in the project

## Next Steps

1. ✅ Implement SendGrid alerting
2. ✅ Build dashboard frontend with report history (`main.py`)
3. ✅ Deploy to Cloud Run — live at the service URL, public dashboard,
   authenticated `/trigger-audit` (see SPEC.md §6 gotcha 5 for the org
   policy override this required)
4. ✅ Set up Cloud Scheduler hourly trigger (`0 9-12 * * *`
   America/Los_Angeles) — `/trigger-audit` no-ops once the day's audit is
   resolved, so this only does real work while retrying a no-quiz day (see
   SPEC.md §4's no-quiz retry policy)
5. ⬜ Record demo video and submit

## Files Reference

| File | Purpose |
|------|---------|
| `requirements.txt` | Python dependencies |
| `daily_audit_pipeline/sendgrid_dispatch.py` | SendGrid email integration |
| `daily_audit_pipeline/reporter.py` | ReporterAgent for Firestore + email |
| `daily_audit_pipeline/agent.py` | Updated to include ReporterAgent in pipeline |
| `main.py` | FastAPI Cloud Run entry point with dashboard |
| `tests/test_evaluator.py` | Test suite (15 tests, all passing) |
| `scripts/test_sendgrid_dispatch.py` | SendGrid dispatch test script |
| `Dockerfile` | Container image definition |
| `cloudbuild.yaml` | Cloud Build pipeline config |
| `deploy.sh` | Automated deployment script |
| `DEPLOYMENT.md` | This file |
