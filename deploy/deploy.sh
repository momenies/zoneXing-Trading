#!/usr/bin/env bash
#
# One-shot deploy of the zoneXing backtest as a Cloud Run Job.
#
#   ./deploy/deploy.sh YOUR_PROJECT_ID [REGION]
#
# Creates (all idempotent — safe to re-run):
#   - an Artifact Registry docker repo
#   - a GCS bucket for results
#   - a dedicated service account with write access to that bucket only
#   - the Cloud Run Job itself
#
# The image is built by Cloud Build, so Docker is NOT needed locally.
set -euo pipefail

PROJECT="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${2:-us-central1}"

if [[ -z "$PROJECT" ]]; then
  echo "usage: $0 PROJECT_ID [REGION]" >&2
  exit 2
fi
command -v gcloud >/dev/null || {
  echo "gcloud not found — install the Google Cloud CLI first:" >&2
  echo "  https://cloud.google.com/sdk/docs/install" >&2
  exit 2
}
gcloud auth print-access-token >/dev/null 2>&1 || {
  echo "not authenticated — run: gcloud auth login" >&2
  exit 2
}

REPO="zonexing"
JOB="zonexing-backtest"
BUCKET="${PROJECT}-zonexing-results"
SA="zonexing-backtest"
SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/backtest:latest"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> project=${PROJECT} region=${REGION}"

echo "==> enabling APIs (first run takes a minute)"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com \
  --project "$PROJECT" --quiet

echo "==> artifact registry"
gcloud artifacts repositories describe "$REPO" \
    --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 || \
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location "$REGION" \
  --description "zoneXing backtest images" --project "$PROJECT" --quiet

echo "==> results bucket gs://${BUCKET}"
gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT" >/dev/null 2>&1 || \
gcloud storage buckets create "gs://${BUCKET}" \
  --location "$REGION" --uniform-bucket-level-access --project "$PROJECT" --quiet

echo "==> service account"
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1 || \
gcloud iam service-accounts create "$SA" \
  --display-name "zoneXing backtest job" --project "$PROJECT" --quiet
# scoped to this bucket only — the job needs nothing else
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/storage.objectAdmin --project "$PROJECT" --quiet >/dev/null

echo "==> building image with Cloud Build"
# --config and --tag are mutually exclusive; the config is required because the
# Dockerfile sits under deploy/ while the build context is the repo root.
gcloud builds submit "$ROOT" \
  --config "${ROOT}/deploy/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE}" \
  --project "$PROJECT" --quiet

echo "==> deploying Cloud Run Job"
JOB_ARGS=(
  --image "$IMAGE"
  --region "$REGION"
  --project "$PROJECT"
  --service-account "$SA_EMAIL"
  --cpu 1 --memory 2Gi
  --max-retries 1
  --task-timeout 30m
  --set-env-vars "GCS_BUCKET=${BUCKET},BARS=52992,INITIAL_CASH=1200,INVEST_FRAC=0.2,DATA_SOURCE=exchange,CACHE_CANDLES=1"
  --quiet
)
if gcloud run jobs describe "$JOB" --region "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud run jobs update "$JOB" "${JOB_ARGS[@]}"
else
  gcloud run jobs create "$JOB" "${JOB_ARGS[@]}"
fi

cat <<DONE

==> deployed.

Run it (streams the log until it finishes):
  gcloud run jobs execute ${JOB} --region ${REGION} --project ${PROJECT} --wait

Read the results:
  gcloud storage cat gs://${BUCKET}/runs/latest/report.txt
  gcloud storage ls  gs://${BUCKET}/runs/

Change the window without rebuilding (e.g. 3 months):
  gcloud run jobs update ${JOB} --region ${REGION} --project ${PROJECT} \\
    --update-env-vars BARS=26496

Tear everything down:
  ./deploy/destroy.sh ${PROJECT} ${REGION}
DONE
