#!/usr/bin/env bash
# Remove everything deploy.sh created.  Usage: ./deploy/destroy.sh PROJECT [REGION]
set -euo pipefail
PROJECT="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${2:-us-central1}"
[[ -z "$PROJECT" ]] && { echo "usage: $0 PROJECT_ID [REGION]" >&2; exit 2; }

BUCKET="${PROJECT}-zonexing-results"
echo "This deletes the Cloud Run job, the image repo, the service account,"
echo "and gs://${BUCKET} INCLUDING ALL RESULTS in project ${PROJECT}."
read -r -p "Type the project id to confirm: " confirm
[[ "$confirm" == "$PROJECT" ]] || { echo "aborted."; exit 1; }

gcloud run jobs delete zonexing-backtest --region "$REGION" --project "$PROJECT" --quiet || true
gcloud artifacts repositories delete zonexing --location "$REGION" --project "$PROJECT" --quiet || true
gcloud iam service-accounts delete "zonexing-backtest@${PROJECT}.iam.gserviceaccount.com" --project "$PROJECT" --quiet || true
gcloud storage rm -r "gs://${BUCKET}" --project "$PROJECT" --quiet || true
echo "done."
