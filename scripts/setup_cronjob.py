#!/usr/bin/env python3
"""
Create (or update) the cron-job.org job that triggers the CBVA Monitor.

GitHub's own cron drops most scheduled runs for this repo, so an external
scheduler fires the workflow's `workflow_dispatch` endpoint instead:
every 15 minutes, 7 AM–9 PM Pacific, Saturday and Sunday (DST handled by
cron-job.org via the job's timezone).

Secrets are read from the environment and never printed:
  CRONJOB_API_KEY     cron-job.org → Settings → API → API key
  GH_DISPATCH_TOKEN   GitHub fine-grained token, this repo only,
                      permission "Actions: Read and write"

Usage:
  python scripts/setup_cronjob.py --dry-run   # show the job, no network
  python scripts/setup_cronjob.py             # create or update the job
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO      = "suacide24/cbva-monitor"
WORKFLOW  = "daily.yml"
API       = "https://api.cron-job.org"
TITLE     = "CBVA Monitor trigger"
MINUTES   = [0, 15, 30, 45]
HOURS     = list(range(7, 22))      # 7:00 AM … 9:45 PM Pacific
WEEKDAYS  = [0, 6]                  # 0 = Sunday, 6 = Saturday
TIMEZONE  = "America/Los_Angeles"


def build_job(gh_token: str) -> dict:
    return {
        "title": TITLE,
        "url": f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        "enabled": True,
        "saveResponses": True,
        "requestMethod": 1,  # POST
        "schedule": {
            "timezone": TIMEZONE,
            "expiresAt": 0,
            "minutes": MINUTES,
            "hours": HOURS,
            "mdays": [-1],
            "months": [-1],
            "wdays": WEEKDAYS,
        },
        "notification": {"onFailure": True, "onSuccess": False, "onDisable": True},
        "extendedData": {
            "headers": {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "cbva-monitor-trigger",
            },
            "body": json.dumps({"ref": "main"}),
        },
    }


def _call(method: str, path: str, api_key: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"cron-job.org API error {e.code} on {method} {path}: {e.read().decode()[:300]}")


def check_dispatch_token(gh_token: str) -> None:
    """Fail early if the GitHub token can't see the workflow."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}",
        headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        sys.exit(f"GH_DISPATCH_TOKEN can't read the workflow (HTTP {e.code}). "
                 "It needs 'Actions: Read and write' on this repo.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        job = build_job("<GH_DISPATCH_TOKEN>")
        print(json.dumps(job, indent=2))
        return

    api_key  = os.environ.get("CRONJOB_API_KEY", "").strip()
    gh_token = os.environ.get("GH_DISPATCH_TOKEN", "").strip()
    if not api_key or not gh_token:
        sys.exit("Set CRONJOB_API_KEY and GH_DISPATCH_TOKEN in the environment first.")

    check_dispatch_token(gh_token)
    job = build_job(gh_token)

    existing = [j for j in _call("GET", "/jobs", api_key).get("jobs", []) if j.get("title") == TITLE]
    if existing:
        job_id = existing[0]["jobId"]
        _call("PATCH", f"/jobs/{job_id}", api_key, {"job": job})
        print(f"Updated cron-job.org job {job_id}.")
    else:
        job_id = _call("PUT", "/jobs", api_key, {"job": job}).get("jobId")
        print(f"Created cron-job.org job {job_id}.")
    print(f"Schedule: every 15 min, {HOURS[0]}:00–{HOURS[-1]}:45 {TIMEZONE}, Sat+Sun.")


if __name__ == "__main__":
    main()
