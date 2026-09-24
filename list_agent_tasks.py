"""
list_agent_tasks.py

Reads the "Agent Tasks" Google Tasks list using a domain-wide-delegated
service account, and parses each task's notes into the
Routine / Status / Prompt convention the orchestrator depends on.

This script only reads and prints -- it's step 3 of the build, proving
API access and the parsing convention work before the orchestrator
starts firing routines and patching task status (that's step 4).

KEYLESS AUTH
------------
There is no service account JSON key anywhere in this flow. The org
enforces constraints/iam.disableServiceAccountKeyCreation, and we want
that policy kept. Instead:

  1. google.auth.default() resolves whoever we are (ADC). On a laptop
     that's your gcloud user creds; on Cloud Run it's the attached
     service account. Same code path either way.
  2. That identity calls IAM Credentials signJwt, asking Google to sign
     a delegation assertion with the service account's Google-managed
     private key -- which we never see.
  3. The signed JWT is exchanged at the OAuth token endpoint for an
     access token scoped to the impersonated Workspace user.

The "sub" claim is what performs the delegation; it replaces the
.with_subject() call used in the old key-based version.

Prereqs (done once, outside this script):
  1. Enable the APIs:
       gcloud services enable tasks.googleapis.com
       gcloud services enable iamcredentials.googleapis.com
  2. Create the service account:
       gcloud iam service-accounts create agent-tasks-orchestrator \
         --display-name="Agent Tasks Orchestrator"
  3. Let the caller sign as that service account. Locally that's you:
       gcloud iam service-accounts add-iam-policy-binding \
         agent-tasks-orchestrator@YOUR_PROJECT_ID.iam.gserviceaccount.com \
         --member="user:you@example.com" \
         --role="roles/iam.serviceAccountTokenCreator"
     On Cloud Run, grant the same role to the attached service account
     (the SA on itself, if it's the same one).
  4. Seed ADC locally (not needed on Cloud Run):
       gcloud auth application-default login
  5. Copy the service account's numeric "Unique ID" (NOT the email):
       gcloud iam service-accounts describe <SA_EMAIL> \
         --format='value(uniqueId)'
  6. In the Workspace Admin console (admin.google.com), go to
     Security > Access and data control > API controls > Domain-wide
     delegation > Add new, and authorize that numeric client ID for:
       https://www.googleapis.com/auth/tasks
     Until this is done, everything above succeeds and the final token
     exchange fails with "unauthorized_client".

Install dependencies:
  pip install -r requirements.txt

Env vars this script expects:
  ORCHESTRATOR_SA_EMAIL   service account to impersonate (the one
                          authorized for domain-wide delegation)
  WORKSPACE_USER_EMAIL    you@example.com -- becomes the
                          JWT "sub" claim
  AGENT_TASKLIST_TITLE    "Agent Tasks" (default, override if needed)

Run it:
  ORCHESTRATOR_SA_EMAIL=agent-tasks-orchestrator@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  WORKSPACE_USER_EMAIL=you@example.com \
  python list_agent_tasks.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import google.auth
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/tasks"]

# The assertion's "aud" must match the endpoint we POST it to.
TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Expects notes shaped like:
#   Routine: Klaviyo Campaign
#   Status: queued          <- optional; absent means queued
#   Prompt:
#   <free-form prompt text, can span multiple lines>
#
# Parsed by splitting on the first "Prompt:" rather than with one regex over
# the whole blob, so header keys can appear in any order and a prompt body
# that happens to contain "Status:" or "Prompt:" can't corrupt the header.


def require_env(name, hint):
    try:
        return os.environ[name]
    except KeyError:
        sys.exit(f"{name} is not set -- {hint}")


def sign_delegation_jwt(sa_email, subject):
    """Have Google sign a delegation assertion with the SA's managed key."""
    # cloud-platform scope is for calling signJwt itself, NOT for Tasks --
    # the Tasks scope goes in the claim set below.
    source_creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    iam = build("iamcredentials", "v1", credentials=source_creds)

    now = int(time.time())
    claims = {
        "iss": sa_email,
        "sub": subject,          # <-- this is the delegation
        "scope": " ".join(SCOPES),
        "aud": TOKEN_URI,
        "iat": now,
        "exp": now + 3600,
    }
    resp = (
        iam.projects()
        .serviceAccounts()
        .signJwt(
            name=f"projects/-/serviceAccounts/{sa_email}",
            body={"payload": json.dumps(claims)},
        )
        .execute()
    )
    return resp["signedJwt"]


def exchange_jwt_for_token(signed_jwt):
    body = urllib.parse.urlencode(
        {"grant_type": JWT_BEARER_GRANT, "assertion": signed_jwt}
    ).encode()
    req = urllib.request.Request(
        TOKEN_URI,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        if "unauthorized_client" in detail:
            sys.exit(
                "Token exchange rejected: unauthorized_client.\n"
                "The signing worked -- domain-wide delegation is what's missing.\n"
                "Authorize the SA's NUMERIC client ID (not its email) at\n"
                "admin.google.com > Security > Access and data control >\n"
                "API controls > Domain-wide delegation, for scope:\n"
                f"  {' '.join(SCOPES)}\n\n"
                f"Raw response: {detail}"
            )
        sys.exit(f"Token exchange failed ({err.code}): {detail}")


def get_tasks_service():
    sa_email = require_env(
        "ORCHESTRATOR_SA_EMAIL",
        "set it to the delegated service account, e.g. "
        "agent-tasks-orchestrator@YOUR_PROJECT_ID.iam.gserviceaccount.com",
    )
    subject = require_env(
        "WORKSPACE_USER_EMAIL",
        "set it to the Workspace user to impersonate, e.g. "
        "you@example.com",
    )
    token = exchange_jwt_for_token(sign_delegation_jwt(sa_email, subject))
    # Access token is good for ~1h, which comfortably outlives a single
    # orchestrator tick. Long-running processes would need a refresh path.
    return build("tasks", "v1", credentials=Credentials(token=token))


def find_tasklist_id(service, title):
    page_token = None
    while True:
        resp = service.tasklists().list(maxResults=100, pageToken=page_token).execute()
        for tl in resp.get("items", []):
            if tl["title"] == title:
                return tl["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    raise LookupError(f'No task list named "{title}" found for this account.')


def parse_task_notes(notes: str):
    if not notes:
        return None

    # Everything after the FIRST "Prompt:" is body, no exceptions.
    head, sep, body = notes.partition("Prompt:")
    if not sep:
        return None

    fields = {}
    for line in head.splitlines():
        key, kv_sep, value = line.strip().partition(":")
        if kv_sep:
            fields[key.strip().lower()] = value.strip()

    routine = fields.get("routine", "").strip().strip('"')
    if not routine:
        return None

    prompt = body.strip()
    if not prompt:
        return None

    return {
        "routine": routine,
        # Status is optional. A hand-written task has no status until the
        # orchestrator stamps one, and defaulting to "queued" means a task
        # can't sit ignored forever just for missing a boilerplate line.
        "status": (fields.get("status") or "queued").lower(),
        "prompt": prompt,
    }


def list_queued_tasks(service, tasklist_id):
    queued = []
    page_token = None
    while True:
        resp = service.tasks().list(
            tasklist=tasklist_id,
            showCompleted=False,
            showHidden=False,
            maxResults=100,
            pageToken=page_token,
        ).execute()
        for task in resp.get("items", []):
            parsed = parse_task_notes(task.get("notes", ""))
            if parsed is None:
                print(f'  [skip] "{task["title"]}" -- notes don\'t match the Routine/Status/Prompt convention')
                continue
            if parsed["status"] != "queued":
                print(f'  [skip] "{task["title"]}" -- status is "{parsed["status"]}", not "queued"')
                continue
            queued.append({
                "id": task["id"],
                "title": task["title"],
                "position": task.get("position", ""),
                **parsed,
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    # "position" is Google Tasks' sortable ordering string -- sort by it
    # so the top-of-list task fires first.
    queued.sort(key=lambda t: t["position"])
    return queued


if __name__ == "__main__":
    tasklist_title = os.environ.get("AGENT_TASKLIST_TITLE", "Agent Tasks")

    service = get_tasks_service()
    tasklist_id = find_tasklist_id(service, tasklist_title)
    print(f'Task list "{tasklist_title}" -> {tasklist_id}\n')

    queued = list_queued_tasks(service, tasklist_id)
    print()
    if not queued:
        print("No queued tasks found.")
    for t in queued:
        preview = t["prompt"][:80] + ("..." if len(t["prompt"]) > 80 else "")
        print(f'- [{t["routine"]}] {t["title"]}  (task id: {t["id"]})')
        print(f'    prompt: {preview}')
