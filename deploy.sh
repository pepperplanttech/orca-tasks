#!/usr/bin/env bash
#
# deploy.sh -- provision and deploy the Orca task orchestrator.
#
# Safe to re-run: every step is idempotent, and Terraform owns the state.
#
# The preflight below is not ceremony. Each check corresponds to a failure
# that actually happened during the first build of this system, and each one
# fails in a way that is genuinely hard to diagnose from the error alone.

set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/infra"
TFVARS="$INFRA_DIR/terraform.tfvars"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }

ask() {  # ask <prompt> <default>
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " reply </dev/tty
    printf '%s' "${reply:-$default}"
  else
    read -r -p "$prompt: " reply </dev/tty
    printf '%s' "$reply"
  fi
}

ask_secret() {  # never echoed, never in shell history
  local prompt="$1" reply
  read -r -s -p "$prompt: " reply </dev/tty
  printf '\n' >&2
  printf '%s' "$reply"
}

pause_for_admin() {
  warn ""
  warn "PAUSED -- an administrator needs to act before this can continue."
  warn ""
  printf '%s\n' "$1"
  warn ""
  read -r -p "Press Enter once that is done to re-check, or Ctrl-C to stop. " </dev/tty
}

# ---------------------------------------------------------------------------
bold "== Tooling =="
# ---------------------------------------------------------------------------
command -v gcloud    >/dev/null || die "gcloud not found. Install the Google Cloud SDK."
command -v terraform >/dev/null || die "terraform not found. Install Terraform >= 1.5."
ok "gcloud $(gcloud version --format='value(\"Google Cloud SDK\")' 2>/dev/null | head -1)"
ok "terraform $(terraform version -json | python3 -c 'import json,sys;print(json.load(sys.stdin)["terraform_version"])')"

# ---------------------------------------------------------------------------
bold ""
bold "== Identity =="
# ---------------------------------------------------------------------------
# The single most confusing failure mode: gcloud stays authenticated as
# whoever you last logged in as. Switching to an admin account to fix billing
# and forgetting to switch back produces PERMISSION_DENIED errors on a
# project you own, which look like org policy problems and are not.
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[[ -n "$ACCOUNT" && "$ACCOUNT" != "(unset)" ]] || die "Not logged in. Run: gcloud auth login"
bold "  Active account: $ACCOUNT"
if [[ "$(ask 'Deploy as this account? (y/n)' 'y')" != "y" ]]; then
  gcloud auth login
  ACCOUNT="$(gcloud config get-value account)"
fi
ok "deploying as $ACCOUNT"

# Terraform does not use the gcloud CLI login above. The provider has no
# credentials of its own, so it falls back to Application Default Credentials,
# a separate login. Missing or mismatched ADC passes every check in this
# script and then fails inside terraform apply with PERMISSION_DENIED.
adc_account() {
  local token
  token="$(gcloud auth application-default print-access-token 2>/dev/null)" || return 1
  # POST, not GET, so the token never lands in a URL.
  curl -s --data-urlencode "access_token=$token" https://oauth2.googleapis.com/tokeninfo \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("email", ""))
except Exception: print("")'
}

while :; do
  if ! ADC_ACCOUNT="$(adc_account)"; then
    warn "  No Application Default Credentials -- Terraform needs them."
  elif [[ -z "$ADC_ACCOUNT" ]]; then
    warn "  Could not read the ADC account; assuming it is $ACCOUNT."
    break
  elif [[ "$ADC_ACCOUNT" == "$ACCOUNT" ]]; then
    ok "Terraform credentials (ADC) match"
    break
  else
    warn "  Terraform credentials (ADC) are $ADC_ACCOUNT, not $ACCOUNT."
  fi
  [[ "$(ask "Run 'gcloud auth application-default login' as $ACCOUNT now? (y/n)" 'y')" == "y" ]] \
    || die "Terraform must run as $ACCOUNT. Run: gcloud auth application-default login"
  gcloud auth application-default login
done

PROJECT="$(ask 'Project ID' "$(gcloud config get-value project 2>/dev/null || echo '')")"
[[ -n "$PROJECT" ]] || die "A project ID is required."
gcloud projects describe "$PROJECT" >/dev/null 2>&1 \
  || die "Project '$PROJECT' not found, or $ACCOUNT cannot see it."
ok "project $PROJECT"

# ---------------------------------------------------------------------------
bold ""
bold "== Permissions =="
# ---------------------------------------------------------------------------
REQUIRED_PERMS=(
  resourcemanager.projects.setIamPolicy
  serviceusage.services.enable
  iam.serviceAccounts.create
  iam.serviceAccounts.setIamPolicy
  secretmanager.secrets.create
  run.jobs.create
  cloudscheduler.jobs.create
)

while :; do
  GRANTED="$(gcloud projects test-iam-permissions "$PROJECT" \
    --permissions="$(IFS=,; echo "${REQUIRED_PERMS[*]}")" \
    --format='value(permissions)' 2>/dev/null | tr ';' '\n' | tr ',' '\n')"
  MISSING=()
  for p in "${REQUIRED_PERMS[@]}"; do
    grep -qx "$p" <<<"$GRANTED" || MISSING+=("$p")
  done
  [[ ${#MISSING[@]} -eq 0 ]] && { ok "all required permissions present"; break; }

  pause_for_admin "$(cat <<EOF
$ACCOUNT is missing these permissions on project $PROJECT:

$(printf '  - %s\n' "${MISSING[@]}")

Ask a project administrator to grant roles/owner, or the narrower set:
  roles/resourcemanager.projectIamAdmin
  roles/serviceusage.serviceUsageAdmin
  roles/iam.serviceAccountAdmin
  roles/secretmanager.admin
  roles/run.admin
  roles/cloudscheduler.admin

  gcloud projects add-iam-policy-binding $PROJECT \\
    --member="user:$ACCOUNT" --role="roles/owner"
EOF
)"
done

# ---------------------------------------------------------------------------
bold ""
bold "== Billing =="
# ---------------------------------------------------------------------------
# Secret Manager, Cloud Run and Scheduler all refuse to enable without it.
while :; do
  BILLING="$(gcloud billing projects describe "$PROJECT" \
    --format='value(billingEnabled)' 2>/dev/null || echo "unknown")"
  [[ "$BILLING" == "True" ]] && { ok "billing linked"; break; }

  pause_for_admin "$(cat <<EOF
No billing account is linked to $PROJECT (or this account cannot read it).

Linking needs TWO permissions held by ONE identity:
  - on the project:         roles/owner or roles/billing.projectManager
  - on the billing account: roles/billing.user or roles/billing.admin

Billing account roles are granted on the BILLING ACCOUNT's own permissions
page (console.cloud.google.com/billing -> Account management), not on the
project's IAM page. A Workspace super admin has neither by default.

  gcloud billing projects link $PROJECT --billing-account=XXXXXX-XXXXXX-XXXXXX

If that reports "Cloud billing quota exceeded", the billing account has hit
its project cap: unlink a dead project, or request an increase at
https://support.google.com/code/contact/billing_quota_increase
EOF
)"
done

# ---------------------------------------------------------------------------
bold ""
bold "== Org policy =="
# ---------------------------------------------------------------------------
# This design is keyless by choice, so a key-creation ban is fine and is
# reported as compatible rather than as a problem.
if gcloud org-policies describe iam.disableServiceAccountKeyCreation \
     --project="$PROJECT" --effective >/dev/null 2>&1; then
  ok "service account key creation is restricted -- fine, this design mints no keys"
else
  ok "no key-creation restriction detected"
fi

# ---------------------------------------------------------------------------
bold ""
bold "== Configuration =="
# ---------------------------------------------------------------------------
if [[ -f "$TFVARS" ]]; then
  bold "  Found existing $TFVARS"
  [[ "$(ask 'Reuse it? (y/n)' 'y')" == "y" ]] && SKIP_CONFIG=1 || SKIP_CONFIG=0
else
  SKIP_CONFIG=0
fi

REGION="$(gcloud config get-value run/region 2>/dev/null || echo us-east4)"
if [[ "$SKIP_CONFIG" -eq 0 ]]; then
  REGION="$(ask 'Region' "${REGION:-us-east4}")"
  WS_USER="$(ask 'Workspace user to impersonate (owns the task list)' "$ACCOUNT")"
  TASKLIST="$(ask 'Task list title' 'Agent Tasks')"
  FOLDER="$(ask 'Brand assets Drive folder ID (blank for none)' '')"
  SCHEDULE="$(ask 'Tick schedule (cron)' '*/30 * * * *')"
  TZ_NAME="$(ask 'Time zone' 'America/New_York')"

  ROUTINES_BLOCK=""
  TOKENS_BLOCK=""
  while :; do
    R_NAME="$(ask 'Routine name, exactly as it appears in a task Routine: line' '')"
    [[ -z "$R_NAME" ]] && break
    R_ID="$(ask "  trig_... id for \"$R_NAME\"" '')"
    R_TOK="$(ask_secret "  API token for \"$R_NAME\" (not echoed)")"
    ROUTINES_BLOCK+=$'  "'"$R_NAME"$'" = {\n    trigger_id = "'"$R_ID"$'"\n  }\n'
    TOKENS_BLOCK+=$'  "'"$R_NAME"$'" = "'"$R_TOK"$'"\n'
    [[ "$(ask 'Add another routine? (y/n)' 'n')" == "y" ]] || break
  done
  [[ -n "$ROUTINES_BLOCK" ]] || die "At least one routine is required."

  umask 077   # tokens are about to land on disk
  cat > "$TFVARS" <<EOF
project_id             = "$PROJECT"
region                 = "$REGION"
deployer_email         = "$ACCOUNT"
workspace_user_email   = "$WS_USER"
tasklist_title         = "$TASKLIST"
brand_assets_folder_id = "$FOLDER"
schedule               = "$SCHEDULE"
time_zone              = "$TZ_NAME"

routines = {
$ROUTINES_BLOCK}

routine_tokens = {
$TOKENS_BLOCK}

image = "IMAGE_PLACEHOLDER"
EOF
  ok "wrote $TFVARS (mode 600, gitignored)"
else
  REGION="$(grep -E '^region' "$TFVARS" | head -1 | cut -d'"' -f2)"
fi

# ---------------------------------------------------------------------------
bold ""
bold "== Enable APIs and create identities =="
# ---------------------------------------------------------------------------
# Targeted first pass: the image cannot be built until Artifact Registry and
# Cloud Build exist, and the job cannot be created until the image exists.
cd "$INFRA_DIR"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve -target=google_project_service.enabled >/dev/null
ok "APIs enabled"
terraform apply -input=false -auto-approve \
  -target=google_project_iam_member.cloudbuild_default_sa >/dev/null
ok "Cloud Build permissions granted"

# ---------------------------------------------------------------------------
bold ""
bold "== Build image =="
# ---------------------------------------------------------------------------
REPO="orca"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/orchestrator:$(date +%Y%m%d-%H%M%S)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location="$REGION" --project="$PROJECT" --quiet >/dev/null
ok "artifact registry ready"

bold "  Building (a few minutes)..."
gcloud builds submit "$(dirname "$INFRA_DIR")" --tag="$IMAGE" --project="$PROJECT" --quiet >/dev/null
ok "pushed $IMAGE"

python3 - "$TFVARS" "$IMAGE" <<'PY'
import re, sys
path, image = sys.argv[1], sys.argv[2]
text = open(path).read()
text = re.sub(r'^image\s*=\s*".*"$', f'image = "{image}"', text, flags=re.M)
open(path, "w").write(text)
PY

# ---------------------------------------------------------------------------
bold ""
bold "== Deploy =="
# ---------------------------------------------------------------------------
terraform apply -input=false
echo
terraform output -raw manual_steps
