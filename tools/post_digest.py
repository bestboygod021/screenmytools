#!/usr/bin/env python3
"""Post one sticky digest comment per profile on its tracking issue.

Reads the JSON produced by ``python -m app.cli digest --profiles --json`` and,
for every profile that names an issue, creates or updates that profile's own
comment (each profile has a distinct HTML marker, so re-runs never stack up).

Usage:
    python -m app.cli digest --profiles --profiles-base DIR --json > plan.json
    python tools/post_digest.py plan.json            # actually post
    python tools/post_digest.py plan.json --dry-run  # show what would be posted
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import digest  # noqa: E402


def plan_entries(plan: dict) -> list[dict]:
    """The profiles worth commenting on: those with an issue number."""
    return [entry for entry in plan.get("profiles", []) if int(entry.get("issue") or 0) > 0]


def comment_for(entry: dict, run_url: str = "") -> tuple[int, str]:
    """``(issue_number, markdown body)`` for one plan entry."""
    text = digest.format_digest(entry)
    return int(entry["issue"]), digest.comment_body(entry.get("profile", "profile"), text, run_url)


def _gh(args: list[str], payload: dict | None = None) -> str:
    command = ["gh", "api", *args]
    if payload is not None:
        command += ["--input", "-"]
        proc = subprocess.run(
            command, input=json.dumps(payload), capture_output=True, text=True, check=False
        )
    else:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def post_or_update(repo: str, issue: int, marker: str, body: str) -> str:
    """Create the comment, or update the one carrying ``marker``. Returns an action word."""
    listing = _gh([f"repos/{repo}/issues/{issue}/comments", "--paginate"])
    comments = json.loads(listing or "[]")
    existing = next((c["id"] for c in comments if marker in (c.get("body") or "")), None)
    if existing:
        _gh(["-X", "PATCH", f"repos/{repo}/issues/comments/{existing}"], {"body": body})
        return "updated"
    _gh(["-X", "POST", f"repos/{repo}/issues/{issue}/comments"], {"body": body})
    return "posted"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="JSON file from 'digest --profiles --json'.")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of posting.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-url", default="")
    args = parser.parse_args(argv)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    entries = plan_entries(plan)
    if not entries:
        print("No profile names a tracking issue; nothing to post.")
        return 0

    if args.dry_run:
        for entry in entries:
            issue, body = comment_for(entry, args.run_url)
            print(f"--- would post to issue #{issue} ---")
            print(body)
        return 0

    if not args.repo:
        print("Set GITHUB_REPOSITORY or pass --repo.", file=sys.stderr)
        return 2
    if shutil.which("gh") is None:
        print("The GitHub CLI ('gh') is required to post comments.", file=sys.stderr)
        return 2

    failed = 0
    for entry in entries:
        issue, body = comment_for(entry, args.run_url)
        marker = digest.comment_marker(entry.get("profile", "profile"))
        try:
            action = post_or_update(args.repo, issue, marker, body)
        except RuntimeError as exc:
            print(f"profile '{entry.get('profile')}' -> issue #{issue}: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"profile '{entry.get('profile')}' -> issue #{issue}: {action}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
