#!/usr/bin/env bash
#
# import.sh -- adopt an existing, hand-built deployment into Terraform state.
#
# Only needed once, for a project whose resources were created with gcloud
# before this config existed. A fresh client deployment should run
# ../deploy.sh instead and never touch this.
#
# Safe to re-run: anything already in state is skipped.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PROJECT="$(grep -E '^project_id' terraform.tfvars | cut -d'"' -f2)"
REGION="$(grep -E '^region'     terraform.tfvars | cut -d'"' -f2)"
ORCH="agent-tasks-orchestrator@${PROJECT}.iam.gserviceaccount.com"
READER="drive-asset-reader@${PROJECT}.iam.gserviceaccount.com"
NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

adopt() {  # adopt <terraform address> <resource id>
  if terraform state show "$1" >/dev/null 2>&1; then
    printf '  = %s (already in state)\n' "$1"
  elif terraform import -input=false "$1" "$2" >/dev/null 2>&1; then
    printf '  + %s\n' "$1"
  else
    printf '  ! %s could not be imported -- may not exist yet\n' "$1"
  fi
}

terraform init -input=false >/dev/null

for svc in iamcredentials tasks drive secretmanager run cloudbuild artifactregistry cloudscheduler; do
  adopt "google_project_service.enabled[\"${svc}.googleapis.com\"]" "$PROJECT/${svc}.googleapis.com"
done

adopt google_service_account.orchestrator  "projects/$PROJECT/serviceAccounts/$ORCH"
adopt google_service_account.drive_reader  "projects/$PROJECT/serviceAccounts/$READER"

adopt google_service_account_iam_member.orchestrator_self_sign \
  "projects/$PROJECT/serviceAccounts/$ORCH roles/iam.serviceAccountTokenCreator serviceAccount:$ORCH"
adopt google_service_account_iam_member.orchestrator_impersonates_drive_reader \
  "projects/$PROJECT/serviceAccounts/$READER roles/iam.serviceAccountTokenCreator serviceAccount:$ORCH"
adopt google_service_account_iam_member.deployer_acts_as_orchestrator \
  "projects/$PROJECT/serviceAccounts/$ORCH roles/iam.serviceAccountUser user:$(grep -E '^deployer_email' terraform.tfvars | cut -d'"' -f2)"

adopt google_project_iam_member.orchestrator_run_invoker \
  "$PROJECT roles/run.invoker serviceAccount:$ORCH"
adopt google_project_iam_member.cloudbuild_default_sa \
  "$PROJECT roles/cloudbuild.builds.builder serviceAccount:${NUM}-compute@developer.gserviceaccount.com"

# Routine names come straight from terraform.tfvars so this stays generic.
while IFS= read -r name; do
  slug="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g')"
  secret="routine-token-${slug}"
  adopt "google_secret_manager_secret.routine_token[\"$name\"]"         "projects/$PROJECT/secrets/$secret"
  adopt "google_secret_manager_secret_version.routine_token[\"$name\"]" "projects/$PROJECT/secrets/$secret/versions/1"
  adopt "google_secret_manager_secret_iam_member.orchestrator_reads_token[\"$name\"]" \
    "projects/$PROJECT/secrets/$secret roles/secretmanager.secretAccessor serviceAccount:$ORCH"
done < <(sed -n '/^routines = {/,/^}/p' terraform.tfvars | grep -oE '"[^"]+" *= *\{' | cut -d'"' -f2)

adopt google_cloud_run_v2_job.orchestrator "projects/$PROJECT/locations/$REGION/jobs/orca-orchestrator"
adopt google_cloud_scheduler_job.tick      "projects/$PROJECT/locations/$REGION/jobs/orca-tick"

echo
echo "Now run: terraform plan   (expect no changes, or reviewable drift)"
