#!/bin/bash

# Quizzy Auditor Cloud Run Deployment Script
# Usage: ./deploy.sh [--project PROJECT_ID] [--region REGION]

set -e

# Configuration
PROJECT_ID="${1:-quizzy-auditor-hackathon}"
REGION="${2:-us-central1}"
SERVICE_NAME="quizzy-auditor"
IMAGE_NAME="${SERVICE_NAME}:latest"
GCR_IMAGE="gcr.io/${PROJECT_ID}/${IMAGE_NAME}"
SCHEDULER_JOB_NAME="quizzy-auditor-daily-trigger"
SCHEDULER_HOUR="9"  # First check, 9 AM America/Los_Angeles
SCHEDULER_RETRY_WINDOW_HOURS=3  # 3 hourly retries after the first check (4 checks total)
SCHEDULER_LAST_HOUR=$((SCHEDULER_HOUR + SCHEDULER_RETRY_WINDOW_HOURS))

echo "📦 Quizzy Auditor Deployment"
echo "=============================="
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Service: ${SERVICE_NAME}"
echo ""

# Step 1: Set the project
echo "1️⃣  Setting GCP project..."
gcloud config set project "${PROJECT_ID}"

# Step 2: Grant IAM permissions
echo "2️⃣  Granting IAM permissions..."
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/cloudbuild.builds.builder" \
    --quiet 2>/dev/null || echo "   (IAM binding already exists)"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/aiplatform.user" \
    --quiet 2>/dev/null || echo "   (IAM binding already exists)"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/datastore.user" \
    --quiet 2>/dev/null || echo "   (IAM binding already exists)"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet 2>/dev/null || echo "   (IAM binding already exists)"

# Step 3: Build and push Docker image
echo "3️⃣  Building and pushing Docker image..."
# COMMIT_SHA is a Cloud Build substitution that's only auto-populated for
# trigger-based builds; a manual `gcloud builds submit` like this one needs
# it passed explicitly or the image tag comes out empty.
BUILD_COMMIT_SHA=$(git rev-parse --short=12 HEAD)
gcloud builds submit . \
    --config=cloudbuild.yaml \
    --substitutions=_REGION="${REGION}",COMMIT_SHA="${BUILD_COMMIT_SHA}"

# Step 4: Deploy to Cloud Run
echo "4️⃣  Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --image="${GCR_IMAGE}" \
    --platform=managed \
    --region="${REGION}" \
    --allow-unauthenticated \
    --memory=2Gi \
    --cpu=2 \
    --timeout=3600 \
    --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_ENTERPRISE=1" \
    --service-account="${COMPUTE_SA}"

# Get the service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --format='value(status.url)')

echo "✅ Cloud Run deployment successful!"
echo "   Service URL: ${SERVICE_URL}"
echo ""

# Step 4.5: Wire SERVICE_URL into the running service. main.py's Cloud
# Scheduler auth check verifies the OIDC token's audience against
# os.environ["SERVICE_URL"] — without this, the URL isn't knowable until
# after the first deploy, so it was never injected and the audience check
# was silently skipped (any valid Google-signed ID token could hit
# /trigger-audit, not just the scheduler job's own service account).
echo "4️⃣.5  Wiring SERVICE_URL into the running service for OIDC audience verification..."
gcloud run services update "${SERVICE_NAME}" \
    --region="${REGION}" \
    --update-env-vars="SERVICE_URL=${SERVICE_URL}"

# Step 5: Create/update Cloud Scheduler job
echo "5️⃣  Setting up Cloud Scheduler..."

# Check if scheduler job exists
if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" --location="${REGION}" &>/dev/null; then
    echo "   Updating existing scheduler job..."
    gcloud scheduler jobs delete "${SCHEDULER_JOB_NAME}" \
        --location="${REGION}" \
        --quiet
fi

gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" \
    --schedule="0 ${SCHEDULER_HOUR}-${SCHEDULER_LAST_HOUR} * * *" \
    --time-zone="America/Los_Angeles" \
    --uri="${SERVICE_URL}/trigger-audit" \
    --http-method=POST \
    --oidc-service-account-email="${COMPUTE_SA}" \
    --oidc-token-audience="${SERVICE_URL}"

echo "✅ Cloud Scheduler job created!"
echo "   Job: ${SCHEDULER_JOB_NAME}"
echo "   Schedule: Hourly ${SCHEDULER_HOUR}:00-${SCHEDULER_LAST_HOUR}:00 America/Los_Angeles"
echo "   (/trigger-audit is a no-op once today's audit is resolved, so this"
echo "   only does real work on a no-quiz day — up to 4 checks before it"
echo "   alerts and stops retrying.)"
echo ""

# Step 6: Verify Secret Manager has the SendGrid API key
echo "6️⃣  Checking Secret Manager for SendGrid API key..."
if ! gcloud secrets describe sendgrid-api-key &>/dev/null; then
    echo "   ⚠️  SendGrid API key not found in Secret Manager — see DEPLOYMENT.md"
    echo "   §4 (Set Up SendGrid Secret) before relying on email alerts."
else
    echo "   ✅ SendGrid API key found in Secret Manager"
fi

echo ""
echo "📊 Deployment Complete!"
echo "========================"
echo "Dashboard: ${SERVICE_URL}"
echo "Cloud Run Service: ${SERVICE_NAME}"
echo "Scheduler Job: ${SCHEDULER_JOB_NAME}"
echo ""
echo "Next steps:"
echo "1. Check the dashboard: ${SERVICE_URL}"
echo "2. Test the trigger manually: curl -X POST '${SERVICE_URL}/trigger-audit' -H \"Authorization: Bearer \$(gcloud auth print-identity-token --audiences=${SERVICE_URL})\""
echo "3. View logs: gcloud run logs read ${SERVICE_NAME} --region=${REGION} --limit=50"
echo "4. Confirm the scheduler job: gcloud scheduler jobs describe ${SCHEDULER_JOB_NAME} --location=${REGION}"
