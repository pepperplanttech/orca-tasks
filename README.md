# Orca

A sequential task-execution agent for Google Tasks, built on Claude Code Routines and Google Cloud.

Orca turns a Google Tasks list into a work queue for an AI agent. Every 30 minutes it takes the topmost ready task, gathers any supporting material the task needs, and dispatches it to a Claude Code Routine. Claude does the work and reports back through a push notification. The task stays open until a person reviews the output and ticks it off.

> Abridged from [docs/Orca-System-Documentation.pdf](docs/Orca-System-Documentation.pdf) (v1.1, 21 September 2026). Project `YOUR_PROJECT_ID`, region `us-east4`.

## Quick start

### 1. Install the tools

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud`)
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.5 or later
- `python3`

You don't need Docker. The container image is built remotely by Cloud Build.

### 2. Authenticate gcloud twice

`deploy.sh` checks the gcloud CLI account, but Terraform authenticates separately through Application Default Credentials. Log in to both with the **same** account:

```bash
gcloud auth login
```

```bash
gcloud auth application-default login
```

`deploy.sh` checks that the two accounts match and offers to redo the second login if they don't. Doing it up front saves a detour.

### 3. Prepare the GCP project

`deploy.sh` doesn't create the project. It checks the project and pauses if something is missing, but you'll save a round trip by having these in place first:

- **The project exists.** Optionally make it the default with `gcloud config set project <id>`.
- **You have `roles/owner` on it,** or the narrower set the script prints.
- **Billing is linked.** This is the step most likely to need someone else; see [the billing trap](#deploying).

### 4. Create the routines in Claude (before deploying)

`deploy.sh` asks for each routine's trigger ID and token, so the routines have to exist first. In the Claude desktop app, go to More → Routines. In the web sidebar, it's under Scheduled. For each routine:

1. Authorize the connectors it needs (for example Klaviyo or Shopify) at the account level, then attach them to the routine.
2. Write the prompt, and make sure it **opts in to the `<routine-fire-payload>` block** (see [Routines](#routines)).
3. Add an **API trigger**, and note its `trig_...` ID and its token, which starts with `sk-ant-oat`.

Note the routine's name exactly as you'll write it on a task's `Routine:` line.

### 5. Have these answers ready

The script prompts for:

| Prompt | Notes |
|---|---|
| Project ID and region | Region defaults to `us-east4`. |
| Workspace user to impersonate | The account that owns the task list. Defaults to you. |
| Task list title | Defaults to `Agent Tasks`. |
| Brand assets Drive folder ID | Optional. It's the last path segment of the folder's URL. |
| Tick schedule and time zone | Defaults to `*/30 * * * *` and `America/New_York`. |
| Each routine's name, `trig_...` ID, and token | The token isn't echoed as you type. |

The answers are saved to `infra/terraform.tfvars` (mode 600, gitignored). On a re-run, the script offers to reuse them.

### 6. Deploy, then finish the manual steps

```bash
./deploy.sh
```

When the script finishes, it prints the steps Terraform can't do. Don't skip the domain-wide delegation step: nothing works until it's done. See [Manual steps after deploy](#manual-steps-after-deploy).

Then add a task to the list (see [Writing a task](#writing-a-task)). Preview what would be sent with `python orchestrator.py --dry-run`, or wait for the next tick. To run the orchestrator locally, you'll first need `pip install -r requirements.txt` and the env vars listed at the top of [orchestrator.py](orchestrator.py).

## Design principles

- **Sequential.** One task runs at a time. The queue does not advance while work is in flight, or until a person has reviewed the result.
- **Human-gated.** Nothing an agent produces goes live on its own. In the reference implementation, campaigns are created as drafts and a person publishes them.
- **Least-privilege.** There are no downloadable credentials anywhere. Each identity can reach only what its job requires, and the platform enforces that, not the prompt.

The first routine builds Klaviyo email campaigns, but the orchestrator knows nothing about email. It reads a task, looks up a routine by name, and fires it. To add a new class of work, you create a routine and name it in a task; the orchestrator needs no code change. Because tasks address routines by name, the set of registered routines is effectively a permission surface.

## Architecture

| Platform | Role |
|---|---|
| **Google Tasks** | The queue and the user interface. There is no separate admin surface. |
| **Google Drive** | Supporting material (brand guidelines, design tokens), read at run time so it can change without a redeploy. |
| **Google Cloud Platform** | The orchestrator: a scheduled container that reads the queue, fetches assets, and dispatches work. |
| **Claude Code Routines** | Execution: a cloud sandbox running Claude with a fixed prompt and a defined set of connectors. |

Each tick starts by *reaping*: any task that has been `running` longer than the timeout is marked `stalled`, on the assumption that the run died or was never reviewed. Otherwise a single dead task would block the queue forever. For details, see [The reaper](#infrastructure).

```
Cloud Scheduler (every 30 min, OAuth)
        ▼
Cloud Run Job, one tick:
  1  reap tasks at "running" past 24h
  2  if any task is still running → stop
  3  take the top "queued" task by position
  4  fetch brand assets                      ──▶ Google Drive
  5  patch Status: running + Started         ──▶ Google Tasks
  6  POST /fire, write the session URL back
        ▼
Claude Code Routine: builds the work as a DRAFT (Klaviyo + Shopify connectors)
        ▼
push notification → you review → tick the task off → queue advances
```

**Status is patched before the routine fires.** The fire endpoint has no idempotency key, so every request creates a new session. If the orchestrator fired first and crashed before recording it, the next tick would fire the task again. Patching first means a crash strands the task instead of duplicating it, and a stranded task is recoverable.

**Retries are disabled** in both Cloud Run and Cloud Scheduler, because a retry could re-run a tick that already fired. The next tick is only 30 minutes away.

## Writing a task

Orca reads one task list, matched by title (default `Agent Tasks`). The task title is only a label for people. The machine-readable state lives in the notes:

```
Routine: Klaviyo Campaign
Status: queued
Started: 2026-09-01T12:30:00+00:00
Session: https://claude.ai/code/session_01GDw...
Prompt:
Create a Klaviyo email campaign based on the concept of a
"Benito's Bundle" — a pre-defined set of three sauces...
```

The parser splits on the first `Prompt:`. Everything before it is read as `Key: value` headers in any order, and everything after it is the prompt body, taken verbatim.

| Field | Written by | Meaning |
|---|---|---|
| `Routine` | You | Exact routine name |
| `Prompt` | You | The assignment. Free-form, multi-line. |
| `Status` | Orchestrator | Optional. An absent or empty status means `queued`. |
| `Started` | Orchestrator | UTC dispatch time, used by the reaper |
| `Session` | Orchestrator | Link to the Claude session transcript |

The status values are:

- `queued`: ready to run.
- `running`: dispatched. Blocks the queue until the task is closed.
- `stalled`: set by the reaper when a task sat at `running` past the timeout.
- `draft`: for parking a half-written task. Notes autosave as you type, so write `Status: draft` first. A task with no `Prompt:` line is also skipped.

**Completion is the native checkbox.** There is no `done` status. Ticking a task off hides it from the orchestrator's scan and releases the queue.

Limits: the notes field holds at most 8,192 characters, so link out for anything longer. Tasks run in list order, top first.

## Drive assets

Brand material is fetched from Drive at dispatch time and injected into the prompt. The `drive-asset-reader` service account has **no** domain-wide delegation. It sees only what is shared with it, at the `drive.readonly` scope.

- Google Docs and Sheets are exported as text. Markdown, JSON, and plain text are downloaded directly.
- Binary assets (logos, photos, PDFs) are reported separately, never silently dropped. In the reference implementation they live in the Klaviyo image library.
- When two files share a filename stem (for example, a `Brand_Voice` Google Doc and `Brand_Voice.md`), the plain file wins. The other is used only if that read fails.
- Sharing a folder inside a shared drive can add the service account to the whole drive, so check the resulting scope after you share.

## Infrastructure

All resources are defined in Terraform under [infra/](infra/), including API enablement and IAM bindings. There is no public endpoint.

| Resource | Purpose |
|---|---|
| `agent-tasks-orchestrator` | Service account with delegated Tasks access. It can sign as itself and impersonate the Drive reader. |
| `drive-asset-reader` | Service account with read-only, folder-scoped Drive access and no delegation |
| `orca-orchestrator` | Cloud Run job, one tick per execution, retries disabled |
| `orca-tick` | Cloud Scheduler job. Invokes the Cloud Run job via OAuth, retries disabled. |
| `routine-token-*` | One Secret Manager secret per routine, mounted as an env var at run time |

**Keyless domain-wide delegation.** The org enforces `constraints/iam.disableServiceAccountKeyCreation`, and that policy was kept deliberately. Instead of a JSON key, Application Default Credentials call IAM Credentials `signJwt`, and Google signs the delegation assertion with its managed key. The signed JWT is then exchanged for an access token for the impersonated user (the `sub` claim).

**The reaper.** Every tick reaps first. A task at `running` longer than `REAPER_TIMEOUT_SECONDS` (default 24h) becomes `stalled`. A `running` task with no usable timestamp is flagged for a person and is never reaped automatically. The timeout covers review time, not run time. A shorter window would release the queue overnight, before anyone had seen the last result.

## Routines

A routine is a saved prompt, a model, a set of MCP connectors, and an API trigger. The orchestrator stores each routine's trigger ID and bearer token, and fires it by POST with an `anthropic-version` header.

The payload has a `BRAND ASSETS` section (each file under a `--- filename ---` header) followed by a `TASK` section. The payload is capped at 65,536 characters; the reference implementation uses about 22,000. It arrives inside a `<routine-fire-payload>` wrapper that marks the contents as data. **The routine prompt must opt in to treating the payload as its assignment, or the run does nothing.**

The reference routine prompt covers:

- what the agent is, and that the payload is its assignment
- where assets come from (Drive is unavailable)
- that imagery comes from the Klaviyo library and products from Shopify
- a tripwire: halt if a connector resolves to the wrong account
- no invented products, prices, or image URLs
- build a draft only: never send, schedule, or publish
- never close the task
- a final report of what was created and what was assumed

**Prompts are not a security boundary.** A prompt only asks the model to behave. The real limits come from the connectors a routine has and the scopes they hold. When something must not happen, remove the capability. This is why Drive access was taken out of the routine and replaced with a read-only pre-fetch.

Connectors inherit the connected account's permissions, including write access. Routines run in a sandbox with a default network allowlist; add any other host under the environment's custom network access settings.

Routine-specific notes live in [docs/routines/](docs/routines/).

## Deploying

```bash
./deploy.sh
```

The script is interactive and idempotent, and Terraform owns the state. Before it changes anything, it checks:

- **Active gcloud account.** A stale login shows up as `PERMISSION_DENIED`, which looks like an org policy problem.
- **Terraform credentials.** Terraform uses Application Default Credentials, not the gcloud CLI login, so the script checks that both are the same account.
- **The seven required permissions.** If any are missing, it prints the fix and waits for you to re-check.
- **Billing.**
- **Org policy.** A key restriction is fine; this design mints no keys.

**The billing trap.** Linking billing needs one identity to hold roles on two different resources:

- on the project: `roles/owner` or `roles/billing.projectManager`
- on the billing account: `roles/billing.user` or `roles/billing.admin`

Billing account roles are granted on the billing account's own permissions page, not in project IAM. A Workspace super admin holds neither role by default. `Cloud billing quota exceeded` means the billing account has hit its project cap.

### Manual steps after deploy

The script prints these with the real values filled in.

1. **Authorize domain-wide delegation.** In Workspace Admin, go to Security → Access and data control → API controls → Domain-wide delegation. Add the orchestrator's numeric **client ID** (not its email) with the scope `https://www.googleapis.com/auth/tasks`. This needs a super admin. Until it is done, the token exchange fails with `unauthorized_client`.
2. **Share the assets folder** with the Drive reader as Viewer, then check the resulting scope.
3. **Create the task list** under the impersonated user, using the configured title.
4. **Check the routines.** You created them before deploying, because the script needs their trigger IDs. Confirm that each prompt opts in to the fire payload.

### Configuration

Routines are configured as a map. Env var and secret names are derived from the routine name. For example, `Klaviyo Campaign` becomes the env var `ROUTINE_ID_KLAVIYO_CAMPAIGN` and the secret `routine-token-klaviyo-campaign`. Tokens are entered without echo and kept out of version control. Tokens that don't start with `sk-ant-oat` are rejected, which catches an API key pasted by mistake.

To bring a hand-built project under Terraform, run `infra/import.sh`. Don't use it for fresh deployments.

### Per-client checklist

- a GCP project with billing (check both halves of the billing permission)
- Workspace super admin access, needed once for delegation
- a Claude account with Routines (desktop: More → Routines; web sidebar: Scheduled)
- connectors authorized at the account level
- an assets folder (optional), shared read-only with the Drive reader
- a tick interval: 30 minutes suits review-gated work, and a shorter interval never adds concurrency

## Operating

```bash
python orchestrator.py --dry-run          # print the exact payload; patch nothing, fire nothing
```

```bash
python orchestrator.py --reset <task_id>  # put a stranded task back to queued
```

```bash
python orchestrator.py --reap-only        # reap stalled tasks and exit
```

To read a run, open the session URL written on the task; the full transcript is there.

## Scope

Orca is deliberately small. It has no database, web UI, user accounts, or public endpoints. The queue is a Google Tasks list, the state is a few lines in a notes field, and the audit trail is the session transcript. That is enough for review-gated work at a handful of tasks per week. Scale it up only when something concrete requires it.
