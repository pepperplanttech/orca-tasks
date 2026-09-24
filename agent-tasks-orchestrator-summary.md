# Agent Tasks Orchestrator — Project Summary

Context doc for a Claude Code session. Goal: an agent that sequentially works
through a Google Tasks list (software dev, Shopify e-commerce, Figma design,
Klaviyo campaigns, etc.), using GCP for scheduling/orchestration and Claude
Code Routines for the actual execution, with a human-in-the-loop checkpoint
whenever something is ready to preview or blocked.

## Architecture

**Execution layer — Claude Code Routines.** A routine is a saved Claude Code
prompt + repo access + MCP connectors, runnable as a full autonomous cloud
session via a schedule, a GitHub event, or an HTTP POST to a per-routine API
trigger endpoint. Routines inherit whatever MCP connectors are already
connected on the account — Figma, Shopify, Klaviyo, and Airtable are already
connected, so a routine needs no extra wiring to use them. Plan: one routine
per task category (e.g. "Dev Tasks", "Shopify Tasks", "Figma Tasks",
"Klaviyo Campaign"), each scoped to only the connectors/repo it needs.

**Orchestration layer — GCP.** GCP's job is glue, not hosting Claude's brain:

- Cloud Scheduler triggers a Cloud Run service/function on an interval
  (e.g. every 10–15 min).
- That function reads the "Agent Tasks" Google Tasks list, finds the next
  queued task, POSTs its prompt to the matching routine's `/fire` endpoint,
  and marks the task `running` so it isn't refired next tick — this is what
  makes execution sequential without a real task queue.

**Human-in-the-loop.** The run reports through the routine's push
notification and stops there. It has no way to write back to the task list.
You review the draft and tick the task off in Google Tasks yourself, which
is what releases the queue. See "Task closure" below for why the callback
design was built and then removed.

**Fallback path (not started):** if Routines' research-preview limits (daily
run cap, no self-hosting) become a problem, the alternative is the Claude
Agent SDK in a container on Cloud Run, with MCP servers configured directly
rather than inherited from the account, state in Firestore, secrets in
Secret Manager. Worth doing only after the Routines-based version is
validated.

## Task convention (Google Tasks "Agent Tasks" list)

- **Title field:** short human-readable summary.
- **Notes field:**

```
Routine: Klaviyo Campaign
Status: queued
Prompt:
<free-form prompt text, can span multiple lines>
```

- `Status` is custom (queued / running / needs_review / done) because the
  Tasks API only natively supports `needsAction` / `completed`.
- **`Status` is OPTIONAL. An absent Status parses as `queued`** -- a
  hand-written task has no status until the orchestrator stamps one, and
  requiring the boilerplate line invites the worst failure mode this system
  has (a task sitting ignored forever because a line was missed). Decided
  2026-08-31 after the first real task turned out to have no Status line. Reserve the
  native `completed` flag for the true end state; keep it `needsAction` for
  everything still in flight so it stays visible on the list.
- Parsing splits on the FIRST `Prompt:`; everything before it is parsed as
  `Key: value` header lines (any order), everything after is body. This
  replaced a single fixed-order regex, which had required Routine-then-Status
  exactly and could be corrupted by a body containing `Status:`/`Prompt:`.
  Both cases are covered by the checks in the module.
- Notes field caps at 8,192 characters — link out to a doc for anything
  longer.

### Parking a task you are still writing (convention, 2026-09-02)

**`Status: draft` means "not ready, do not fire."** Write that line FIRST,
then compose the rest of the notes.

This convention exists because the obvious instinct — leave Status blank
until it's ready — does the opposite of what you'd expect. An absent Status
parses as `queued`, which is the deliberate choice made 2026-08-31 above, so
a half-written task is an armed task. `Status:` with an empty value after the
colon is also `queued`, because the empty string is falsy and falls through
to the same default. Google Tasks autosaves as you type and the tick is every
30 minutes, so the exposure is real rather than theoretical.

The gate is a literal `!= "queued"`, so any unrecognised value is inert;
`draft` is just the agreed one. Avoid `running`, `stalled` and `failed - …`,
which are the orchestrator's own vocabulary — `running` in particular would
hold the sequential gate until the 24h reaper cleared it.

A second, stronger guard, useful for a long task: a task with no `Prompt:`
line, or with `Prompt:` and an empty body, is unparseable and skipped no
matter what Status says. Writing the prompt body before adding the `Prompt:`
line is belt and braces.

Known sharp edge, deliberately not code: a present-but-empty `Status:` is
indistinguishable from an absent one only by convention, not by the parser.
Treating it as "someone is mid-edit" rather than `queued` would be a small,
defensible change to `parse_task_notes`, but it narrows the 2026-08-31
decision, so it wants a deliberate choice rather than a drive-by fix.

## Auth approach

Domain-wide delegation, **keyless**. The org enforces
`constraints/iam.disableServiceAccountKeyCreation` (set org-wide
2025-05-17), and we deliberately kept it rather than carving out a
project-level exception -- an override would re-enable downloadable keys
for every SA in the project permanently, and would not revoke any key
already minted.

Instead of a JSON key, Google signs the delegation assertion:

1. `google.auth.default()` resolves the caller (ADC). Locally that's the
   gcloud user creds for the Workspace user; on Cloud Run it's the attached service
   account. **Identical code path in both** -- no key file, no
   `GOOGLE_APPLICATION_CREDENTIALS`.
2. That identity calls IAM Credentials `signJwt` on
   `agent-tasks-orchestrator@...`, which signs with the SA's
   Google-managed private key (never exposed). Caller needs
   `roles/iam.serviceAccountTokenCreator` on the SA.
3. The signed JWT is POSTed to `https://oauth2.googleapis.com/token` with
   `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, returning an
   access token scoped to the impersonated user (~1h lifetime).

Claim set: `iss` = SA email, **`sub` = the Workspace user's email (this
is the delegation, replacing `.with_subject()`)**, `scope` = the tasks
scope, `aud` = the token endpoint (must match where it's POSTed), plus
`iat`/`exp`.

Note the two different scopes in play: `cloud-platform` authorizes the
`signJwt` call itself; the Tasks scope lives inside the claim set.

Still required regardless of signer: authorize the SA's numeric client ID
in the Workspace Admin console. Until then, signing succeeds and the token
exchange fails with `unauthorized_client` -- the script traps that error
specifically and says so.

Older reference material (incl. salrashid.dev, 2021) shows the legacy
`accounts.google.com/o/oauth2/token` endpoint with `grant_type=assertion`;
the code uses the current JWT-bearer form.

## Runtime

Python 3.13 — GA on Cloud Run / Cloud Run functions, matches the local dev
environment (`python3.13 -m venv venv`), and has broader package/wheel
coverage right now than the newer 3.14 GA runtime for libraries like
`google-auth` and `google-api-python-client`.

- Custom container: `FROM python:3.13-slim` in the Dockerfile.
- Source-based deploy / Cloud Run functions: `python313` runtime, or
  `--base-image google-22/python313` on `gcloud run deploy`.

## Script: `list_agent_tasks.py`

Read-only proof-of-concept for step 3 (Tasks API access + parsing
convention). Lists queued tasks in "Agent Tasks" and parses each one's
Routine/Status/Prompt. Does not yet patch status or fire routines — that's
step 4.

See the file itself -- it is no longer duplicated here, since the
embedded copy went stale the moment auth changed from key-based to
keyless.


## Task closure: no callback (decided 2026-09-01)

**A run cannot close its own task. A human checks it off in the Google Tasks
UI after reviewing the draft.**

An earlier design had the routine POST to a public Cloud Run webhook,
authorised by a stateless HMAC capability token minted per dispatch. It was
built and tested green -- forged, expired, cross-task and replayed requests
all rejected, and `Routine`/`Prompt` writes refused -- then removed.

Two reasons it went:

1. **It was blocked anyway.** The org enforces Domain Restricted Sharing
   (`iam.allowedPolicyMemberDomains`), so `allUsers` cannot be granted
   `roles/run.invoker` and the endpoint could not be made public. The only
   fix was a project-scoped org policy exception, which would let *anything*
   in the project be shared publicly.

2. **It automated the wrong step.** Every campaign needs human review before
   it sends. A callback closes out the task without anyone looking at the
   draft, which removes the one gate the design exists to enforce.

Polling was evaluated as an alternative and ruled out with evidence: the
routine's `sk-ant-oat01` token is fire-only. `/v1/code/sessions` and
`/v1/code/triggers/{id}` return 401 for it, and `/v1/claude_code/...` read
paths return 404. Reading session status needs account-level OAuth
credentials, and parking those in Cloud Run is a far larger standing-secret
risk than the webhook ever was.

**How closure works now:** the orchestrator patches `Status: running` and
stamps `Started`. The run does its work and reports via push notification.
You review the draft and tick the task off in Google Tasks, which sets
`completed`/`hidden` -- so it drops out of the scan entirely and the next
tick fires the next queued task. No status editing required.

## Sequential gate and the reaper

`orchestrator.py` reaps before it fires, every tick:

- Any task at `running` older than `REAPER_TIMEOUT_SECONDS` (default 24h)
  is marked `stalled` -- deliberately distinct from `failed`, which means a
  run reported failure. `stalled` means nobody reported anything.
- A `running` task with no usable timestamp is flagged for a human, never
  reaped. Reaping on no evidence could kill a live run.
- If anything is still `running` after the reap, the tick fires nothing.
  One campaign at a time, each reviewable before the next.

The 24h default covers *review* latency, not run time. Reaping at four hours
would release the gate overnight and fire the next campaign before anyone
looked at the last one. The cost is that a genuinely dead run blocks the
queue for a day; at a couple of campaigns a week that is the right trade.
`--reap-only` and `--timeout N` are available for a dedicated schedule.

## Status

**Step 3 COMPLETE (2026-08-31).** `list_agent_tasks.py` runs green against
the real "Agent Tasks" list using keyless
domain-wide delegation -- no service account key exists anywhere. Verified
end to end: ADC -> signJwt -> JWT-bearer exchange -> Tasks API read ->
correct parse of a real Klaviyo task.

**Step 4 COMPLETE (2026-09-01).** `orchestrator.py` runs the full loop
against the live list: reap, sequential gate, fetch brand assets, patch
`Status: running` + `Started`, fire the routine, write the session URL back.
One real Klaviyo campaign draft produced end to end.

Also done: architecture decided, task convention defined, closure model
decided (no callback), Drive access hard-scoped to a read-only service
account, runtime pinned to Python 3.13, GCP project provisioned with billing
and Secret Manager.

## GCP project (provisioned 2026-08-31)

- Keyless auth wired: `iamcredentials.googleapis.com` enabled, and
  the deploying Workspace user granted
  `roles/iam.serviceAccountTokenCreator` on the SA.
- Project: `YOUR_PROJECT_ID`, under the organisation's node. Deliberately
  separate from other projects so domain-wide delegation doesn't live
  alongside unrelated workloads.
- `tasks.googleapis.com` enabled.
- Service account `agent-tasks-orchestrator@YOUR_PROJECT_ID.iam.gserviceaccount.com`
  created, no IAM roles granted (correct -- all its authority comes from
  impersonating the Workspace user, not from project permissions).
- Domain-wide delegation is authorised by the SA's **numeric client ID**
  (this, not the email, is what admin.google.com needs).

## Next steps

- Deploy `orchestrator.py` as a Cloud Run **job** (Dockerfile written;
  entrypoint is the orchestrator, no web server) and drive it with Cloud
  Scheduler. Decide the tick interval — 15-30 min is ample.
- Set up the remaining routines (Dev / Shopify / Figma) with their prompts
  and scoped connectors. "Klaviyo Campaign" is live.
- Consider testing `permitted_tools` on a connector — unverified, but it may
  allow restricting a connector to read-only tools server-side.

**Not doing:** notification webhook, Cloud Function callback, Custom network
access allowlisting. Superseded by manual closure.
