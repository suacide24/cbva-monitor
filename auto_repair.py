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

    # Build Claude prompt — ask for targeted patches, not the whole file.
    # monitor.py is ~1200 lines; returning the complete file risks hitting
    # max_tokens and producing a truncated (syntax-broken) output.
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
3. Return your fix as a JSON array of replacement objects. Each object has:
   - "old": the exact string to find in monitor.py (must match character-for-character,
     including indentation and newlines)
   - "new": the replacement string

HARD RULES:
- Return ONLY the JSON array. No explanation, no markdown fences, nothing else.
- The very first character of your response must be `[`.
- Each "old" string must appear exactly once in the source.
- Do not change logic that is currently working — fix only what is broken.
- Prefer the smallest possible change: a single line fix is better than replacing
  an entire function.
- If the raw API response shows a schema change, update only the relevant parsing
  code to match the actual response structure.

Example response format:
[
  {{"old": "    for candidate in PLAYER_NAMES:", "new": "    for candidate in player_names:"}}
]
"""

    print("[repair] Calling Claude...")
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4000,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = ""
    for block in message.content:
        if block.type == "text":
            response_text = block.text.strip()
            break

    if not response_text:
        die("Claude returned an empty response.")
    if not response_text.startswith("["):
        die(
            f"Claude response is not a JSON array (does not start with '[').\n"
            f"First 300 chars: {response_text[:300]}"
        )

    try:
        patches = json.loads(response_text)
    except json.JSONDecodeError as e:
        die(f"Claude returned invalid JSON: {e}\nResponse: {response_text[:500]}")

    if not isinstance(patches, list) or not patches:
        die(f"Claude returned an empty or non-list patch set: {response_text[:300]}")

    # Apply patches to source
    fixed_source = source
    for i, patch in enumerate(patches):
        old = patch.get("old", "")
        new = patch.get("new", "")
        if not old:
            die(f"Patch {i} has empty 'old' field.")
        count = fixed_source.count(old)
        if count == 0:
            die(f"Patch {i} 'old' string not found in {MONITOR_FILE}:\n{old!r}")
        if count > 1:
            die(f"Patch {i} 'old' string matches {count} locations — must be unique:\n{old!r}")
        fixed_source = fixed_source.replace(old, new, 1)
        print(f"[repair] Patch {i+1}/{len(patches)} applied.")

    # Validate syntax before touching the real file
    try:
        with open(TMP_FILE, "w") as f:
            f.write(fixed_source)
        result = subprocess.run(
            ["python3", "-m", "py_compile", TMP_FILE],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            die(f"Patched source has syntax errors:\n{result.stderr}")
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
