#!/usr/bin/env python3
"""
CBVA Player Monitor
Scrapes CBVA player profiles to detect new tournament registrations
and rating changes. Sends a daily email digest when changes are found.

Env vars (set as GitHub Secrets or in a local .env file):
  PLAYER_NAMES   — comma-separated list, e.g. "John Smith,Jane Doe"
  EMAIL_FROM     — Gmail address to send from
  EMAIL_PASSWORD — Gmail App Password (not your login password)
  EMAIL_TO       — address to receive alerts (defaults to EMAIL_FROM)
"""

import asyncio
import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()  # no-op in GitHub Actions; reads .env locally

PLAYER_NAMES = [n.strip() for n in re.split(r"[,\n]", os.environ.get("PLAYER_NAMES", "")) if n.strip()]
EMAIL_FROM   = os.environ.get("EMAIL_FROM", "")
EMAIL_TO     = os.environ.get("EMAIL_TO") or EMAIL_FROM
EMAIL_PASS   = os.environ.get("EMAIL_PASSWORD", "")
STATE_FILE   = "state.json"
BASE_URL     = "https://cbva.com"

# CBVA rating ladder — lower index = lower skill
RATING_ORDER = ["N", "U", "B", "A", "AA", "AAA", "Open"]


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"players": {}}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, html: str) -> None:
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASS]):
        print("[email] Credentials missing — printing to stdout instead.\n")
        print(f"Subject: {subject}\n{html}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(EMAIL_FROM, EMAIL_PASS)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"[email] Sent: {subject}")


# ── CBVA scraping ─────────────────────────────────────────────────────────────

async def find_profile_url(page, name: str) -> str | None:
    """Search CBVA for a player by name and return their /profile/{id} path."""
    await page.goto(f"{BASE_URL}/search")
    await page.wait_for_selector("input", timeout=10_000)
    await page.fill("input", name)
    await page.keyboard.press("Enter")

    # Wait for profile links to appear, fall back to fixed delay if none arrive
    try:
        await page.wait_for_selector("a[href*='/profile/']", timeout=8_000)
    except Exception:
        await page.wait_for_timeout(3_000)

    for link in await page.query_selector_all("a[href*='/profile/']"):
        text = (await link.inner_text()).strip()
        if all(part.lower() in text.lower() for part in name.split()):
            href = await link.get_attribute("href")
            print(f"  [search] Matched '{text}' -> {href}")
            return href

    # Fallback: first result
    links = await page.query_selector_all("a[href*='/profile/']")
    if links:
        href = await links[0].get_attribute("href")
        print(f"  [search] No exact match for '{name}', using first result -> {href}")
        return href

    print(f"  [search] No results for '{name}'")
    return None


def parse_profile_text(text: str) -> dict:
    """
    Parse plain-text from a CBVA profile page.

    Structure:
        Rating / A / Rank / 1234 / Points / 50
        Upcoming Tournaments
        [Tournament Name]
        [Month Day, Year]
        [Beach, City]
        [Status]          <- optional (Waitlisted #N, Registered, etc.)
        [Division]
        [Partner Name]
        [Own Name]
        Results
        ...
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    rating = rank = "?"
    for i, line in enumerate(lines):
        if line == "Rating" and i + 1 < len(lines):
            rating = lines[i + 1]
        if line == "Rank" and i + 1 < len(lines):
            rank = lines[i + 1]

    try:
        start = lines.index("Upcoming Tournaments") + 1
        end   = next(
            (i for i in range(start, len(lines)) if lines[i] == "Results"),
            len(lines),
        )
        block = lines[start:end]
    except ValueError:
        block = []

    date_re = re.compile(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+,\s+\d{4}$"
    )
    div_re = re.compile(r"(Men's|Women's|Coed)\s+\w+")

    upcoming = []
    i = 0
    while i < len(block):
        line = block[i]
        if date_re.match(line) or div_re.search(line):
            i += 1
            continue

        t = {"name": line, "date": "", "location": "", "status": "", "division": "", "partner": ""}
        i += 1

        if i < len(block) and date_re.match(block[i]):
            t["date"] = block[i]; i += 1

        if i < len(block) and "," in block[i] and not date_re.match(block[i]):
            t["location"] = block[i]; i += 1

        if i < len(block) and any(
            kw in block[i] for kw in ("Waitlist", "Register", "Confirmed", "Accepted")
        ):
            t["status"] = block[i]; i += 1

        if i < len(block) and div_re.search(block[i]):
            t["division"] = block[i]; i += 1

        if i < len(block):
            t["partner"] = block[i]; i += 1

        if i < len(block):
            i += 1  # skip player's own name

        if t["name"]:
            upcoming.append(t)

    return {"rating": rating, "rank": rank, "upcoming_tournaments": upcoming}


async def scrape_profile(page, profile_url: str, debug: bool = False) -> dict:
    full = f"{BASE_URL}{profile_url}" if profile_url.startswith("/") else profile_url
    await page.goto(full)
    await page.wait_for_timeout(2_500)
    text = await page.evaluate("() => document.body.innerText")
    if debug:
        print("  [debug] Raw page text (first 80 lines):")
        for i, line in enumerate(text.split("\n")[:80]):
            print(f"  {i:03}: {repr(line)}")
    data = parse_profile_text(text)
    data["profile_url"] = profile_url
    return data


# ── Change detection ──────────────────────────────────────────────────────────

def rating_rank(r: str) -> int:
    try:
        return RATING_ORDER.index(r)
    except ValueError:
        return -1

def tournament_key(t: dict) -> str:
    return f"{t.get('name')}-{t.get('date')}-{t.get('division')}"


# ── Email template ────────────────────────────────────────────────────────────

def build_html(alerts: list) -> str:
    sections = ""
    for a in alerts:
        sections += (
            f"<h2 style='margin:1.5em 0 .4em;font-size:17px'>"
            f"<a href='{BASE_URL}{a['profile_url']}' style='color:#1a2a4a;text-decoration:none'>"
            f"{a['name']}</a></h2>"
        )

        if rc := a.get("rating_change"):
            icon = "UP" if rc["increased"] else "~"
            sections += (
                f"<p style='margin:.25em 0'>[{icon}] Rating: "
                f"<strong>{rc['from']} -> {rc['to']}</strong></p>"
            )

        for t in a["new_tournaments"]:
            sections += f"""
            <div style='border-left:3px solid #1D9E75;padding:8px 14px;margin:6px 0;
                        background:#f5faf8;border-radius:0 6px 6px 0;font-size:14px'>
              <strong>New signup: {t['name']}</strong><br>
              <span style='color:#555'>{t['date']} - {t['location']}</span><br>
              <span style='color:#555'>{t['division']} - {t['status']} - Partner: {t['partner']}</span>
            </div>"""

    today = datetime.now().strftime("%B %d, %Y")
    return f"""
    <html><body style='font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#222'>
      <h1 style='font-size:20px;border-bottom:2px solid #1a2a4a;padding-bottom:8px;margin-bottom:0'>
        CBVA Player Alert - {today}
      </h1>
      {sections}
      <hr style='margin-top:2em;border:none;border-top:1px solid #ddd'>
      <p style='font-size:11px;color:#aaa'>cbva-monitor - <a href='{BASE_URL}/search' style='color:#aaa'>cbva.com/search</a></p>
    </body></html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    if not PLAYER_NAMES:
        print("PLAYER_NAMES is empty. Set the env var and try again.")
        return

    print(f"Monitoring {len(PLAYER_NAMES)} player(s): {', '.join(PLAYER_NAMES)}")
    state  = load_state()
    alerts = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        for name in PLAYER_NAMES:
            print(f"\nChecking: {name}")
            prev = state.get("players", {}).get(name, {})

            profile_url = prev.get("profile_url") or await find_profile_url(page, name)
            if not profile_url:
                print(f"  Skipping - profile not found.")
                continue

            cur = await scrape_profile(page, profile_url, debug=(name == PLAYER_NAMES[0]))
            print(f"  Rating: {cur['rating']}  Upcoming: {len(cur['upcoming_tournaments'])}")

            alert: dict = {
                "name":            name,
                "profile_url":     profile_url,
                "rating_change":   None,
                "new_tournaments": [],
            }

            # Rating change
            if prev.get("rating") and prev["rating"] != cur["rating"]:
                alert["rating_change"] = {
                    "from":      prev["rating"],
                    "to":        cur["rating"],
                    "increased": rating_rank(cur["rating"]) > rating_rank(prev["rating"]),
                }
                print(f"  Rating changed: {prev['rating']} -> {cur['rating']}")

            # New tournament registrations
            prev_keys = {tournament_key(t) for t in prev.get("upcoming_tournaments", [])}
            for t in cur["upcoming_tournaments"]:
                if tournament_key(t) not in prev_keys:
                    alert["new_tournaments"].append(t)
                    print(f"  New signup: {t['name']} on {t['date']}")

            if alert["rating_change"] or alert["new_tournaments"]:
                alerts.append(alert)

            state.setdefault("players", {})[name] = {
                **cur,
                "last_checked": datetime.now().isoformat(),
            }

        await browser.close()

    save_state(state)
    print(f"\nState saved to {STATE_FILE}")

    if alerts:
        subject = f"CBVA Alert - {datetime.now().strftime('%b %d, %Y')}"
        send_email(subject, build_html(alerts))
        print(f"Alerts found for {len(alerts)} player(s).")
    else:
        print("No changes detected - no email sent.")


if __name__ == "__main__":
    asyncio.run(run())
