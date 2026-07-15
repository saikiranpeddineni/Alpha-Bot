from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any

import yaml

EVENT_KEYS = {
    "CLAIM": {"worker-run", "lease-until", "role", "work-item", "slice", "files", "state", "head"},
    "RECLAIM": {"worker-run", "lease-until", "role", "work-item", "slice", "expired-worker-run", "reason", "head"},
    "HANDOFF": {"worker-run", "role", "slice", "action-taken", "test-commands", "test-results", "surviving-defect", "next-action"},
    "ABORT": {"worker-run", "role", "slice", "reason", "surviving-defect", "next-action"},
    "RECOVERY": {"worker-run", "incident-code", "work-item", "slice", "terminated-worker-runs", "pre-recovery-head", "post-recovery-head", "branch-change-classification", "attributed-worker-run", "recovery-action", "next-state"},
}
STATE_LABELS = {"state:ready", "state:claimed", "state:patching", "state:needs-validation", "state:ci-red", "state:blocked", "state:merge-ready", "state:ledger-invalid"}
FENCE = re.compile(r"```yaml\s*\n(.*?)\n```", re.S | re.I)


class LedgerError(Exception):
    def __init__(self, code: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(code)


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return dt.astimezone(timezone.utc)


def parse_events(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for comment in comments:
        for raw in FENCE.findall(comment.get("body", "")):
            event_type = raw.splitlines()[0].strip()
            if event_type not in EVENT_KEYS:
                continue
            try:
                payload = yaml.safe_load("\n".join(raw.splitlines()[1:]))
            except yaml.YAMLError as exc:
                raise LedgerError("SCHEMA_INVALID", comment_id=comment.get("id"), error=str(exc)) from exc
            if not isinstance(payload, dict):
                raise LedgerError("SCHEMA_INVALID", comment_id=comment.get("id"), error="event payload must be a mapping")
            missing = EVENT_KEYS[event_type] - payload.keys()
            extra = payload.keys() - EVENT_KEYS[event_type]
            if missing or extra:
                raise LedgerError("SCHEMA_INVALID", event=event_type, missing=sorted(missing), extra=sorted(extra))
            try:
                parse_time(str(payload["worker-run"]))
            except (ValueError, TypeError) as exc:
                raise LedgerError("SCHEMA_INVALID", event=event_type, error="invalid worker-run") from exc
            if event_type in {"CLAIM", "RECLAIM"}:
                try:
                    start = parse_time(str(payload["worker-run"]))
                    lease = parse_time(str(payload["lease-until"]))
                except (ValueError, TypeError) as exc:
                    raise LedgerError("SCHEMA_INVALID", event=event_type, error="invalid lease timestamp") from exc
                if int((lease - start).total_seconds()) != 55 * 60:
                    raise LedgerError("SCHEMA_INVALID", event=event_type, error="lease must be exactly 55 minutes")
                if not re.fullmatch(r"[0-9a-f]{40}", str(payload["head"])):
                    raise LedgerError("SCHEMA_INVALID", event=event_type, error="head must be 40-char lowercase SHA")
            events.append({"type": event_type, **payload, "_comment_id": comment.get("id")})
    return events


def validate(events: list[dict[str, Any]], labels: set[str], remote_head: str, now: datetime) -> None:
    terminated = set()
    for event in events:
        if event["type"] in {"HANDOFF", "ABORT"}:
            terminated.add(str(event["worker-run"]))
        elif event["type"] == "RECOVERY":
            terminated.update(map(str, event["terminated-worker-runs"]))

    active = []
    for event in events:
        if event["type"] not in {"CLAIM", "RECLAIM"} or str(event["worker-run"]) in terminated:
            continue
        if parse_time(str(event["lease-until"])) <= now:
            raise LedgerError("LEASE_ORPHANED", work_item=event.get("work-item"), slice=event["slice"], worker_run=event["worker-run"])
        if event["head"] != remote_head:
            raise LedgerError("HEAD_MISMATCH", work_item=event.get("work-item"), slice=event["slice"], worker_run=event["worker-run"], declared_head=event["head"], remote_head=remote_head)
        active.append(event)

    seen = {}
    for event in active:
        key = (str(event.get("work-item")), event["slice"])
        if key in seen:
            raise LedgerError("OVERLAPPING_ACTIVE_CLAIMS", work_item=key[0], slice=key[1], active_claim_1=seen[key]["worker-run"], active_claim_2=event["worker-run"])
        seen[key] = event

    states = labels & STATE_LABELS
    if len(states) != 1:
        raise LedgerError("STATE_DIVERGENCE", labels=sorted(states), expected="exactly one state label")
    if active and states.isdisjoint({"state:claimed", "state:patching"}):
        raise LedgerError("STATE_DIVERGENCE", labels=sorted(states), expected="state:claimed or state:patching while lease active")


def github(path: str, token: str) -> Any:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--issue", type=int)
    args = parser.parse_args()
    token = os.environ["GITHUB_TOKEN"]
    issues = [github(f"/repos/{args.repo}/issues/{args.issue}", token)] if args.issue else github(f"/repos/{args.repo}/issues?state=open&per_page=100", token)
    try:
        for issue in issues:
            if "pull_request" in issue:
                continue
            labels = {item["name"] for item in issue["labels"]}
            if not labels & STATE_LABELS:
                continue
            comments = github(f"/repos/{args.repo}/issues/{issue['number']}/comments?per_page=100", token)
            events = parse_events(comments)
            blocks = FENCE.findall(issue.get("body", ""))
            body = yaml.safe_load(blocks[0]) if blocks else {}
            branch = (body or {}).get("branch")
            if not branch:
                continue
            ref = github(f"/repos/{args.repo}/git/ref/heads/{branch}", token)
            validate(events, labels, ref["object"]["sha"], datetime.now(timezone.utc))
    except LedgerError as exc:
        print("LEDGER_INVALID")
        print(f"code={exc.code}")
        for key, value in exc.details.items():
            print(f"{key}={value}")
        return 1
    print("LEDGER_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
