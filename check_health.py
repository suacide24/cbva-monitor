#!/usr/bin/env python3
"""
CBVA Monitor health check.

Reads state.json and exits 0 (healthy) or 1 (unhealthy) with a human-readable
report. Called by the health_check.yml workflow after every monitor run.

Checks:
  1. Workflow conclusion — did GitHub Actions fail before the script even ran?
  2. Last run status — did the Python script itself report errors?
  3. Liveness (dead-man's switch) — when did the monitor last *succeed*?
     Queried from the GitHub Actions API (not a committed timestamp), so it
     covers weekday daily runs as well as weekend tournament-hour runs.

Structured for Layer 4: state["last_run"]["errors"] contains rich diagnostic
context (traceback, raw CBVA response) that a Claude repair agent can consume.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

STATE_FILE = "state.json"
PT         = timezone(timedelta(hours=-7))

# Staleness thresholds (minutes since last *successful* run).
#
# The monitor is *scheduled* every 30 min on weekend tournament hours, but
# GitHub's scheduled-cron is unreliable: even mid-afternoon, runs routinely
# land 80-110 min apart and occasionally skip for several hours. There's also
# an ~11h overnight window (03:00-14:00 UTC) with no scheduled runs at all.
# So the active-hours threshold must tolerate several missed slots — it should
# fire on a *sustained* outage, not on normal scheduler jitter — and we only
# apply it well clear of the overnight gap (see staleness_threshold).
ACTIVE_MAX_MIN = 240     # weekend daytime: alert only after ~4h of silence
DAILY_MAX_MIN  = 1560    # otherwise: the daily run fires every 24h (+ buffer)


def last_successful_run_age_min(now_utc: datetime | None = None) -> float | None:
    """
    Minutes since the most recent successful "CBVA Monitor" (daily.yml) run,
    via the GitHub Actions API. Returns None when it can't be determined
    (no token/repo env, or API/network failure) so we never false-alarm.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo  = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    url = (f"https://api.github.com/repos/{repo}"
           f"/actions/workflows/daily.yml/runs?status=success&per_page=1")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:                       # network, auth, JSON, etc.
        print(f"  (liveness check skipped — API error: {e})")
        return None
    runs = data.get("workflow_runs") or []
    if not runs:
        return None
    created = runs[0].get("created_at")
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    return (now_utc - dt).total_seconds() / 60


def staleness_threshold(now_pt: datetime) -> int:
    """
    Allowed minutes since the last success, depending on when we are.
    The tight active-hours window is 9 AM–8 PM PT: starting at 9 (not 7) keeps
    the morning health check — which runs right after the overnight no-run gap —
    on the lenient threshold so it doesn't false-alarm before the day's runs
    have had a chance to fire.
    """
    is_weekend = now_pt.weekday() in (5, 6)
    is_active  = 9 <= now_pt.hour <= 20
    return ACTIVE_MAX_MIN if (is_weekend and is_active) else DAILY_MAX_MIN


def evaluate_health(state: dict, workflow_conclusion: str,
                    age_min: float | None, now_pt: datetime) -> list[str]:
    """Pure health evaluation — returns a list of issue strings (empty = healthy)."""
    issues: list[str] = []
    last_run = state.get("last_run", {})

    # Check 1: the workflow itself failed (env set by health_check.yml)
    if workflow_conclusion == "failure":
        issues.append(
            "GitHub Actions workflow failed before the Python script completed "
            "(check Actions logs for install/setup errors)"
        )

    # Check 2: script-level errors
    if last_run.get("status") == "error":
        errs = last_run.get("errors", [])
        summary = "; ".join(f"{e['phase']}: {e['message'][:80]}" for e in errs[:3])
        issues.append(
            f"Last run reported {last_run.get('error_count', '?')} error(s): {summary}"
        )

    # Check 3: liveness / dead-man's switch (API-based, all days)
    if age_min is not None:
        threshold = staleness_threshold(now_pt)
        if age_min > threshold:
            issues.append(
                f"No successful monitor run in {age_min:.0f} min "
                f"(expected ≤{threshold} min) — the scheduler may be stuck"
            )

    return issues


def main() -> None:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"UNHEALTHY\n  • Cannot read {STATE_FILE}: {e}")
        sys.exit(1)

    now_pt  = datetime.now(PT)
    age_min = last_successful_run_age_min()
    issues  = evaluate_health(
        state,
        os.environ.get("WORKFLOW_CONCLUSION", ""),
        age_min,
        now_pt,
    )

    age_str = f"{age_min:.0f} min ago" if age_min is not None else "unknown"
    if issues:
        print("UNHEALTHY")
        for issue in issues:
            print(f"  • {issue}")
        print(f"\n  last successful run: {age_str}")
        sys.exit(1)
    else:
        print(f"HEALTHY — last successful run: {age_str}")
        sys.exit(0)


if __name__ == "__main__":
    main()
