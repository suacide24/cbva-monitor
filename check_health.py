#!/usr/bin/env python3
"""
CBVA Monitor health check.

Reads state.json and exits 0 (healthy) or 1 (unhealthy) with a human-readable
report. Called by the health_check.yml workflow after every monitor run.

Checks:
  1. Last run status — did the Python script itself report errors?
  2. Workflow conclusion — did GitHub Actions fail before the script even ran?
  3. Timing — on tournament days, is the last run recent enough?

Structured for Layer 4: state["last_run"]["errors"] contains rich diagnostic
context (traceback, raw CBVA response) that a Claude repair agent can consume.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

STATE_FILE = "state.json"
PT         = timezone(timedelta(hours=-7))
MAX_AGE_MIN = 75  # expect a run every 30 min; allow generous buffer


def main() -> None:
    # ── Read state ────────────────────────────────────────────────────────────
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _fail(f"Cannot read {STATE_FILE}: {e}")

    issues: list[str] = []
    last_run = state.get("last_run", {})

    # ── Check 1: workflow itself failed (env set by health_check.yml) ─────────
    if os.environ.get("WORKFLOW_CONCLUSION") == "failure":
        issues.append(
            "GitHub Actions workflow failed before the Python script completed "
            "(check Actions logs for install/setup errors)"
        )

    # ── Check 2: script-level errors ──────────────────────────────────────────
    if last_run.get("status") == "error":
        errs = last_run.get("errors", [])
        summary = "; ".join(
            f"{e['phase']}: {e['message'][:80]}" for e in errs[:3]
        )
        issues.append(
            f"Last run reported {last_run.get('error_count', '?')} error(s): {summary}"
        )

    # ── Check 3: stale run on a tournament day ────────────────────────────────
    now    = datetime.now(PT)
    is_weekend        = now.weekday() in (5, 6)  # 5=Sat, 6=Sun
    is_tourney_hours  = 7 <= now.hour <= 20       # 7 AM – 8 PM PT
    if is_weekend and is_tourney_hours:
        ts = last_run.get("timestamp")
        if not ts:
            issues.append("No runs recorded yet today during tournament hours")
        else:
            try:
                last_dt = datetime.fromisoformat(ts)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=PT)
                age_min = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds() / 60
                if age_min > MAX_AGE_MIN:
                    issues.append(
                        f"Last run was {age_min:.0f} min ago "
                        f"(expected ≤{MAX_AGE_MIN} min during tournament hours)"
                    )
            except ValueError:
                issues.append(f"Cannot parse last_run timestamp: {ts!r}")

    # ── Report ────────────────────────────────────────────────────────────────
    if issues:
        print("UNHEALTHY")
        for issue in issues:
            print(f"  • {issue}")
        sha = last_run.get("git_sha", "unknown")
        ts  = last_run.get("timestamp", "never")
        print(f"\n  last_run: {ts}  git_sha: {sha}")
        sys.exit(1)
    else:
        ts  = last_run.get("timestamp", "never")
        sha = last_run.get("git_sha", "unknown")
        print(f"HEALTHY — last run: {ts}  git_sha: {sha}")
        sys.exit(0)


def _fail(msg: str) -> None:
    print(f"UNHEALTHY\n  • {msg}")
    sys.exit(1)


if __name__ == "__main__":
    main()
