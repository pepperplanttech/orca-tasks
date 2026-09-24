"""
orchestrator.py -- step 4.

Takes the top queued task off the "Agent Tasks" list, fetches the brand
assets, fires the matching Claude Code routine, and writes the result back
onto the task.

Ordering matters: Status is patched to "running" BEFORE the routine is
fired. The /fire endpoint has no idempotency key -- every request creates a
new session -- so a crash between the two steps must strand the task rather
than risk firing it twice. A stranded task is recoverable by hand; two live
Klaviyo drafts from one task is a mess. The reaper below makes stranding
self-healing.

A task leaves "running" when YOU check it off in the Google Tasks UI after
reviewing the draft. There is no callback: a run cannot close its own task.
That is deliberate -- every campaign needs human review before it sends, so
automating the close would only automate away the step that matters.

Env vars (beyond those list_agent_tasks.py needs):
  BRAND_ASSETS_FOLDER_ID          Drive folder holding the brand files
  DRIVE_READER_SA_EMAIL           read-only SA to impersonate (local only)
  ROUTINE_TOKEN_<ROUTINE_NAME>    per-routine bearer token, name uppercased
                                  with non-alphanumerics as underscores
                                  e.g. ROUTINE_TOKEN_KLAVIYO_CAMPAIGN
  ROUTINE_ID_<ROUTINE_NAME>       the trig_... id for that routine

  REAPER_TIMEOUT_SECONDS          override the stale-"running" window

Run it:
  python orchestrator.py --dry-run    # show what would happen, touch nothing
  python orchestrator.py              # reap, then fire the top queued task
  python orchestrator.py --reap-only  # reap and exit
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request

import drive_assets
from list_agent_tasks import (
    find_tasklist_id,
    get_tasks_service,
    list_queued_tasks,
    parse_task_notes,
)

FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{trigger_id}/fire"

# The fire payload has a hard character cap. Leave room for the task prompt
# and the framing text so brand assets can never crowd them out.
FIRE_TEXT_LIMIT = 65_536
ASSETS_BUDGET = 40_000


def env_key(prefix, routine_name):
    """"Klaviyo Campaign" -> ROUTINE_TOKEN_KLAVIYO_CAMPAIGN"""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", routine_name).strip("_").upper()
    return f"{prefix}_{slug}"


def render_notes(fields, prompt):
    """Rebuild a task's notes from parsed fields plus the untouched prompt.

    Field order is fixed so the notes stay readable in the Tasks UI as the
    orchestrator rewrites them; any keys we don't know about are preserved
    rather than dropped.
    """
    lines = []
    for key in ("routine", "status", "started", "session"):
        if fields.get(key):
            lines.append(f"{key.title()}: {fields[key]}")
    for key, value in fields.items():
        if key not in ("routine", "status", "started", "session") and value:
            lines.append(f"{key.title()}: {value}")
    lines.append("Prompt:")
    return "\n".join(lines) + "\n" + prompt


def read_fields(notes):
    """Header fields as a dict, plus the prompt body, from raw notes."""
    head, _, body = notes.partition("Prompt:")
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    return fields, body.strip()


def patch_task(service, tasklist_id, task_id, **updates):
    """Set header fields on a task, leaving the prompt body untouched."""
    task = service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    fields, prompt = read_fields(task.get("notes", ""))
    fields.update(updates)
    task["notes"] = render_notes(fields, prompt)
    return service.tasks().update(
        tasklist=tasklist_id, task=task_id, body=task
    ).execute()


def load_brand_assets():
    folder_id = os.environ.get("BRAND_ASSETS_FOLDER_ID")
    if not folder_id:
        return "", []
    service = drive_assets.get_drive_service()
    documents, _skipped, _superseded = drive_assets.fetch_brand_assets(
        service, folder_id
    )
    block = drive_assets.render_assets_block(documents)
    if len(block) > ASSETS_BUDGET:
        raise SystemExit(
            f"Brand assets are {len(block)} chars, over the {ASSETS_BUDGET} "
            "budget. Trim the folder or raise ASSETS_BUDGET."
        )
    return block, [d["name"] for d in documents]


def build_fire_text(assets_block, prompt):
    parts = []
    if assets_block:
        parts.append("BRAND ASSETS\n" + assets_block)
    parts.append("TASK\n" + prompt)
    text = "\n\n".join(parts)
    if len(text) > FIRE_TEXT_LIMIT:
        raise SystemExit(f"Fire payload is {len(text)} chars, over the limit.")
    return text


def fire_routine(trigger_id, token, text):
    req = urllib.request.Request(
        FIRE_URL.format(trigger_id=trigger_id),
        data=json.dumps({"text": text}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        raise SystemExit(
            f"Fire failed ({err.code}). The task is left at status "
            f'"running" on purpose -- reset it by hand once you know why.\n'
            f"Response: {detail}"
        )


def session_url_of(response):
    """Pull the session link out of the fire response, whatever it's called."""
    for key in ("claude_code_session_url", "session_url", "url"):
        if response.get(key):
            return response[key]
    session_id = response.get("session_id") or response.get("id")
    if session_id:
        return f"https://claude.ai/code/{session_id}"
    return ""


# A run that dies -- sandbox crash, network cut, a model that simply stops --
# never reports anything, and its task sits at "running" forever.
# Nothing else in the system notices, because "running" is exactly what a
# healthy in-flight task looks like. The reaper is what closes that hole.
#
# The timeout is deliberately generous. A task leaves "running" when a human
# checks it off after reviewing the draft, so the window has to cover review
# latency, not just run time -- reaping at four hours would release the
# sequential gate overnight and fire the next campaign before anyone looked
# at the last one. A dead run therefore blocks the queue for a day, which at
# a couple of campaigns a week is the right trade.
REAP_AFTER_SECONDS = int(os.environ.get("REAPER_TIMEOUT_SECONDS", 24 * 60 * 60))

# Distinct from "failed", which means the run reported failure. "stalled"
# means nobody reported anything at all -- a different problem with a
# different fix, and worth being able to tell apart at a glance.
STALLED = "stalled"


def scan_tasks(service, tasklist_id):
    """Every parseable task on the list, with its raw record attached."""
    found, page_token = [], None
    while True:
        resp = service.tasks().list(
            tasklist=tasklist_id,
            showCompleted=False,
            showHidden=False,
            maxResults=100,
            pageToken=page_token,
        ).execute()
        for task in resp.get("items", []):
            notes = task.get("notes", "")
            parsed = parse_task_notes(notes)
            if parsed:
                # parse_task_notes only surfaces the three fields the queue
                # cares about; the reaper needs Started, so take the full
                # header too.
                header, _ = read_fields(notes)
                found.append({"raw": task, **header, **parsed})
        page_token = resp.get("nextPageToken")
        if not page_token:
            return found


def started_at(entry):
    """When this task went to "running".

    Prefers the Started stamp the orchestrator writes. Falls back to the
    task's own updated time, because a task that predates the stamp still
    needs reaping -- and patching to "running" is what set that timestamp
    anyway, so it's a close approximation rather than a guess.
    """
    stamp = entry.get("started")
    if stamp:
        try:
            return datetime.fromisoformat(stamp)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(entry["raw"]["updated"])
    except (KeyError, ValueError):
        return None


def reap_stale(service, tasklist_id, timeout_seconds=None):
    """Mark stranded "running" tasks as stalled. Returns what it reaped."""
    timeout = timedelta(
        seconds=timeout_seconds
        if timeout_seconds is not None
        else REAP_AFTER_SECONDS
    )
    cutoff = datetime.now(timezone.utc) - timeout

    reaped = []
    for entry in scan_tasks(service, tasklist_id):
        if entry["status"] != "running":
            continue
        began = started_at(entry)
        if began is None:
            # No usable timestamp at all. Reaping on no evidence could kill a
            # live run, so surface it and let a human decide.
            print(f'  [!] "{entry["raw"]["title"]}" is running with no '
                  "timestamp -- reap it by hand if it's dead")
            continue
        if began < cutoff:
            patch_task(service, tasklist_id, entry["raw"]["id"], status=STALLED)
            reaped.append(entry["raw"])
            age = datetime.now(timezone.utc) - began
            print(f'  reaped "{entry["raw"]["title"]}" '
                  f"(running {int(age.total_seconds() // 60)} min) -> {STALLED}")
    return reaped


def running_tasks(service, tasklist_id):
    return [e for e in scan_tasks(service, tasklist_id) if e["status"] == "running"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reap-only",
        action="store_true",
        help="reap stranded tasks and exit without firing anything",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help=f"seconds before a running task is stalled (default {REAP_AFTER_SECONDS})",
    )
    parser.add_argument(
        "--reset",
        metavar="TASK_ID",
        help="put a stranded task back to queued, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the payload and stop; patch nothing, fire nothing",
    )
    args = parser.parse_args()

    tasks = get_tasks_service()
    tasklist_id = find_tasklist_id(
        tasks, os.environ.get("AGENT_TASKLIST_TITLE", "Agent Tasks")
    )

    if args.reset:
        patch_task(tasks, tasklist_id, args.reset, status="queued", session="")
        print(f"{args.reset} -> queued")
        return

    # Reap first. A tick that skips this would see a dead task as live and
    # refuse to do anything, which is how a queue quietly stops forever.
    print("Reaping stale tasks...")
    reap_stale(tasks, tasklist_id, args.timeout)

    if args.reap_only:
        return

    # Sequential execution is the whole point of this design -- one campaign
    # at a time, each reviewable before the next. Anything still running after
    # the reap is genuinely in flight, so this tick does nothing.
    still_running = running_tasks(tasks, tasklist_id)
    if still_running:
        titles = ", ".join(f'"{e["raw"]["title"]}"' for e in still_running)
        print(f"Still running, not firing: {titles}")
        return

    queued = list_queued_tasks(tasks, tasklist_id)
    if not queued:
        print("No queued tasks.")
        return

    # One task per tick. The whole point is sequential execution with a human
    # able to look at each result before the next one starts.
    task = queued[0]
    print(f'Next: [{task["routine"]}] {task["title"]}  ({task["id"]})')

    token_key = env_key("ROUTINE_TOKEN", task["routine"])
    id_key = env_key("ROUTINE_ID", task["routine"])
    token, trigger_id = os.environ.get(token_key), os.environ.get(id_key)
    if not token or not trigger_id:
        sys.exit(
            f'No routine wired up for "{task["routine"]}". '
            f"Set {token_key} and {id_key}."
        )

    assets_block, asset_names = load_brand_assets()
    print(f"Brand assets: {', '.join(asset_names) or '(none)'}")

    text = build_fire_text(assets_block, task["prompt"])
    print(f"Payload: {len(text)} chars -> {trigger_id}")

    if args.dry_run:
        print("\n--- dry run, nothing fired ---\n")
        print(text[:2000] + ("..." if len(text) > 2000 else ""))
        return

    # Patch first. See the module docstring for why this order is load-bearing.
    patch_task(
        tasks,
        tasklist_id,
        task["id"],
        status="running",
        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    print("Status -> running")

    response = fire_routine(trigger_id, token, text)
    url = session_url_of(response)
    patch_task(tasks, tasklist_id, task["id"], session=url)
    print(f"Fired. Session: {url or '(no url in response)'}")


if __name__ == "__main__":
    main()
