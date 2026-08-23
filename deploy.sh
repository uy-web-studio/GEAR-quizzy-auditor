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
SCHEDULER_TIME="09:00"  # 9 AM America/Los_Angeles daily

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
gcloud builds submit . \
    --config=cloudbuild.yaml \
    --substitutions=_REGION="${REGION}"

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
    --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_ENTERPRISE=1,SKIP_AUTH=true" \
    --service-account="${COMPUTE_SA}"

# Get the service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --format='value(status.url)')

echo "✅ Cloud Run deployment successful!"
echo "   Service URL: ${SERVICE_URL}"
echo ""

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
    --schedule="0 ${SCHEDULER_TIME} * * *" \
    --time-zone="America/Los_Angeles" \
    --uri="${SERVICE_URL}/trigger-audit" \
    --http-method=POST \
    --oidc-service-account-email="${COMPUTE_SA}" \
    --oidc-token-audience="${SERVICE_URL}"

echo "✅ Cloud Scheduler job created!"
echo "   Job: ${SCHEDULER_JOB_NAME}"
echo "   Schedule: Daily at ${SCHEDULER_TIME} America/Los_Angeles"
echo ""

# Step 6: Create/update Secret Manager secret (if needed)
echo "6️⃣  Checking Secret Manager for SendGrid API key..."
if ! gcloud secrets describe sendgrid-api-key &>/dev/null; then
    echo "   ⚠️  SendGrid API key not found in Secret Manager!"
    echo "   To add it, run:"
    echo "   gcloud secrets create sendgrid-api-key --replication-policy=automatic --data-file=- <<< 'YOUR_SENDGRID_API_KEY'"
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
echo "1. Add SendGrid API key to Secret Manager if not already done"
echo "2. Test the deployment: curl -X POST '${SERVICE_URL}/trigger-audit?env=SKIP_AUTH=true'"
echo "3. View logs: gcloud run logs read ${SERVICE_NAME} --region=${REGION} --limit=50"
