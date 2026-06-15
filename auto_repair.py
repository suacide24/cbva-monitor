#!/usr/bin/env python3
"""
Layer 4: Claude-powered auto-repair for monitor.py.

Reads structured error context from state.json, sends it together with the
full monitor.py source to Claude, applies the suggested fix, validates Python
syntax, commits, and pushes. Sends a result email either way.
"""
import json
import os
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic

STATE_FILE   = "state.json"
MONITOR_FILE = "monitor.py"
TMP_FILE     = "_repair_candidate.py"
EMAIL_FROM   = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS   = os.environ.get("EMAIL_PASSWORD", "")
PT           = timezone(timedelta(hours=-7))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Load error context
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception as e:
        die(f"Cannot read {STATE_FILE}: {e}")

    last_run = state.get("last_run", {})
    errors   = last_run.get("errors", [])

    if not errors:
        print("[repair] No structured errors in state.json — nothing to repair.")
        sys.exit(0)

    try:
        with open(MONITOR_FILE) as f:
            source = f.read()
    except Exception as e:
        die(f"Cannot read {MONITOR_FILE}: {e}")

    # Build Claude prompt
    error_json = json.dumps(errors, indent=2)
    prompt = f"""You are an automated repair agent for a Python script called monitor.py.

This script monitors CBVA beach volleyball players via the CBVA tRPC API, runs
on GitHub Actions every 30 minutes on weekends, and emails results to subscribers.
It failed during its last run with the following structured errors:

<errors>
{error_json}
</errors>

The errors include:
- phase: which stage of the script failed (login, roster_scan, ratings_check, etc.)
- exception_type and message: the Python exception
- traceback: full Python traceback
- cbva_response_sample: the raw API response that triggered the parse failure (if any)

Here is the complete current source of monitor.py:

<source>
{source}
</source>

Your task:
1. Diagnose the root cause from the traceback and raw API response.
2. Determine the minimal, targeted fix.
3. Return the COMPLETE fixed monitor.py — every line, from the shebang to EOF.

HARD RULES:
- Your response must be ONLY the raw Python source. No markdown, no fences, no
  explanation text, no prefix, no suffix.
- The very first character must be `#` (the shebang line).
- Do not add new imports or dependencies beyond what is already in the file.
- Do not change logic that is currently working — fix only what is broken.
- If the raw API response shows a schema change, update only the relevant parsing
  code to match the actual response structure.
"""

    print("[repair] Calling Claude...")
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )

    fixed_source = ""
    for block in message.content:
        if block.type == "text":
            fixed_source = block.text.strip()
            break

    if not fixed_source:
        die("Claude returned an empty response.")
    if not fixed_source.startswith("#"):
        die(
            f"Claude response does not start with '#' — likely included explanation text.\n"
            f"First 300 chars: {fixed_source[:300]}"
        )

    # Validate syntax before touching the real file
    try:
        with open(TMP_FILE, "w") as f:
            f.write(fixed_source)
        result = subprocess.run(
            ["python3", "-m", "py_compile", TMP_FILE],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            die(f"Claude's fix has syntax errors:\n{result.stderr}")
    finally:
        if os.path.exists(TMP_FILE):
            os.unlink(TMP_FILE)

    # Apply the fix
    with open(MONITOR_FILE, "w") as f:
        f.write(fixed_source)
    print(f"[repair] Applied fix to {MONITOR_FILE}.")

    # Commit and push
    error_phase = errors[0].get("phase", "unknown") if errors else "unknown"
    commit_msg  = f"fix(auto-repair): {error_phase} error in monitor.py"

    subprocess.run(["git", "config", "user.name",  "cbva-repair[bot]"],  check=True)
    subprocess.run(["git", "config", "user.email", "cbva-repair@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", MONITOR_FILE], check=True)

    diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
    if diff.returncode == 0:
        print("[repair] Fix produced no diff — file was already correct?")
        sys.exit(0)

    subprocess.run(["git", "commit", "-m", commit_msg], check=True)

    pushed = False
    for attempt in range(1, 4):
        result = subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[repair] Pull attempt {attempt} failed: {result.stderr}")
        push = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push.returncode == 0:
            pushed = True
            break
        print(f"[repair] Push attempt {attempt} failed — retrying...")
        time.sleep(5 + attempt * 3)

    if not pushed:
        die("Committed fix locally but all push attempts failed.")

    print(f"[repair] Committed and pushed: {commit_msg}")
    _send_email(
        subject="✅ CBVA Monitor Auto-Repaired",
        html=_success_html(errors, commit_msg),
    )
    print("[repair] Done.")


# ── Email helpers ─────────────────────────────────────────────────────────────

def _success_html(errors: list[dict], commit_msg: str) -> str:
    ts  = datetime.now(PT).strftime("%Y-%m-%d %H:%M PT")
    err = errors[0] if errors else {}

    rows = [
        ("Time",        ts),
        ("Error phase", err.get("phase",          "?")),
        ("Error type",  err.get("exception_type", "?")),
        ("Commit",      f"<code>{commit_msg}</code>"),
    ]
    row_html = "".join(
        f"<tr><td style='padding:6px 10px;background:#f5f5f5;font-weight:600;"
        f"width:120px'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{v}</td></tr>"
        for k, v in rows
    )

    return f"""<html><body style='font-family:sans-serif;max-width:640px;
margin:0 auto;padding:24px;color:#222'>
<h1 style='font-size:20px;border-bottom:2px solid #1D9E75;padding-bottom:8px;
color:#1D9E75'>✅ CBVA Monitor Auto-Repaired</h1>
<p>Claude detected and patched an error in <code>monitor.py</code>.
The fix has been committed to main — the next scheduled run will use it.</p>
<table style='border-collapse:collapse;width:100%;margin:16px 0;font-size:13px'>
{row_html}
</table>
<p style='font-size:12px;color:#888'>
<a href='https://github.com/suacide24/cbva-monitor/actions'>View Actions →</a></p>
</body></html>"""


def _failure_html(msg: str) -> str:
    ts = datetime.now(PT).strftime("%Y-%m-%d %H:%M PT")
    return f"""<html><body style='font-family:sans-serif;max-width:640px;
margin:0 auto;padding:24px;color:#222'>
<h1 style='font-size:20px;border-bottom:2px solid #cc3333;padding-bottom:8px;
color:#cc3333'>❌ CBVA Auto-Repair Failed</h1>
<p>The auto-repair agent could not apply a fix at {ts}.
Manual intervention required.</p>
<pre style='background:#f5f5f5;padding:14px;border-radius:6px;font-size:12px;
white-space:pre-wrap'>{msg}</pre>
<p style='font-size:12px;color:#888'>
<a href='https://github.com/suacide24/cbva-monitor/actions'>View Actions →</a></p>
</body></html>"""


def _send_email(subject: str, html: str) -> None:
    if not all([EMAIL_FROM, EMAIL_PASS]):
        print(f"[email] No credentials — skipping. Subject: {subject}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_FROM
    msg.attach(MIMEText(html, "html"))
    for attempt in range(3):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(EMAIL_FROM, EMAIL_PASS)
                s.sendmail(EMAIL_FROM, EMAIL_FROM, msg.as_string())
            print(f"[email] Sent: {subject}")
            return
        except Exception as e:
            print(f"[email] Attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(5)
    print("[email] All email attempts failed.")


def die(msg: str) -> None:
    print(f"[repair] FATAL: {msg}")
    _send_email(subject="❌ CBVA Auto-Repair Failed", html=_failure_html(msg))
    sys.exit(1)


if __name__ == "__main__":
    main()
