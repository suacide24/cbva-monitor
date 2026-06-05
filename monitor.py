#!/usr/bin/env python3
"""
CBVA Player Monitor
Scrapes CBVA player profiles to detect new tournament registrations,
rating changes, and status updates. Sends a daily email digest when
changes are found, plus a day-of alert when watched players are competing.

Env vars (set as GitHub Secrets or in a local .env file):
  EMAIL_FROM     — Gmail address to send from
  EMAIL_PASSWORD — Gmail App Password (not your login password)
  EMAIL_TO       — address to receive alerts (defaults to EMAIL_FROM)
  PLAYER_NAMES   — comma/newline-separated list (loaded from gist in Actions)
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

load_dotenv()

PLAYER_NAMES = [n.strip() for n in re.split(r"[,\n]", os.environ.get("PLAYER_NAMES", "")) if n.strip()]
EMAIL_FROM   = os.environ.get("EMAIL_FROM", "")
EMAIL_TO     = os.environ.get("EMAIL_TO") or EMAIL_FROM
EMAIL_PASS   = os.environ.get("EMAIL_PASSWORD", "")
STATE_FILE   = "state.json"
BASE_URL     = "https://cbva.com"

RATING_ORDER = ["N", "U", "B", "A", "AA", "AAA", "Open"]
DEBUG        = os.environ.get("CBVA_DEBUG") == "1"


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
    await page.goto(f"{BASE_URL}/search", wait_until="networkidle")
    await page.wait_for_selector("input", timeout=10_000)
    await page.fill("input", name)
    await page.keyboard.press("Enter")

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

    Expected structure when upcoming tournaments exist:
        Rating / A / Rank / 1234 / Points / 50
        Upcoming Tournaments
        [Tournament Name]
        [Month Day, Year]
        [Beach, City]
        [Status]          <- optional (Waitlisted #N, Registered, Confirmed, etc.)
        [Division]
        [Partner Name]
        [Own Name]
        Results
        ...
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    rating = rank = "?"
    for i, line in enumerate(lines):
        if line == "Rating" and i + 1 < len(lines) and lines[i + 1] in RATING_ORDER:
            rating = lines[i + 1]
        if line == "Rank" and i + 1 < len(lines):
            try:
                int(lines[i + 1])
                rank = lines[i + 1]
            except ValueError:
                pass

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
            kw in block[i] for kw in ("Waitlist", "Register", "Confirm", "Accept")
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


async def scrape_profile(page, profile_url: str, debug_name: str | None = None) -> dict:
    full = f"{BASE_URL}{profile_url}" if profile_url.startswith("/") else profile_url

    api_responses: list[tuple[str, str]] = []

    async def capture_response(response):
        url = response.url
        if any(k in url for k in ("tournament", "registr", "upcoming", "cbva.com/api")):
            try:
                body = await response.text()
                api_responses.append((url, body[:500]))
            except Exception:
                api_responses.append((url, "<unreadable>"))

    if DEBUG and debug_name:
        page.on("response", capture_response)

    await page.goto(full, wait_until="networkidle")

    if DEBUG and debug_name:
        if api_responses:
            print(f"  [debug] Intercepted {len(api_responses)} API response(s):")
            for url, body in api_responses:
                print(f"    {url}")
                print(f"    {body[:200]}")
        else:
            print("  [debug] No tournament-related API responses intercepted.")

        await page.screenshot(path=f"debug_{debug_name}.png", full_page=True)
        print(f"  [debug] Screenshot: debug_{debug_name}.png")

        text_all = await page.evaluate("() => document.body.innerText")
        print(f"  [debug] Full innerText ({len(text_all.splitlines())} lines):")
        for i, line in enumerate(text_all.splitlines()):
            print(f"  {i:03}: {repr(line)}")

        # Also dump all visible links
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a,button,[role=tab]'))
                       .map(el => el.textContent.trim())
                       .filter(t => t)
        """)
        print(f"  [debug] Clickable elements: {links}")

    text = await page.evaluate("() => document.body.innerText")
    data = parse_profile_text(text)
    data["profile_url"] = profile_url
    return data


# ── Change detection helpers ──────────────────────────────────────────────────

def rating_rank(r: str) -> int:
    try:
        return RATING_ORDER.index(r)
    except ValueError:
        return -1

def tournament_key(t: dict) -> str:
    return f"{t.get('name')}-{t.get('date')}-{t.get('division')}"

def parse_tournament_date(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(date_str.strip(), "%b %d, %Y")
    except ValueError:
        return None


# ── Email templates ───────────────────────────────────────────────────────────

_CARD = """
<div style='border-left:3px solid {color};padding:8px 14px;margin:6px 0;
            background:{bg};border-radius:0 6px 6px 0;font-size:14px'>
  <strong>{title}</strong><br>
  <span style='color:#555'>{line2}</span><br>
  <span style='color:#555'>{line3}</span>
</div>"""

def _player_header(name: str, profile_url: str) -> str:
    return (
        f"<h2 style='margin:1.5em 0 .4em;font-size:17px'>"
        f"<a href='{BASE_URL}{profile_url}' style='color:#1a2a4a;text-decoration:none'>"
        f"{name}</a></h2>"
    )

def _wrap(body: str, title: str) -> str:
    today = datetime.now().strftime("%B %d, %Y")
    return f"""
    <html><body style='font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#222'>
      <h1 style='font-size:20px;border-bottom:2px solid #1a2a4a;padding-bottom:8px;margin-bottom:0'>
        {title} — {today}
      </h1>
      {body}
      <hr style='margin-top:2em;border:none;border-top:1px solid #ddd'>
      <p style='font-size:11px;color:#aaa'>cbva-monitor · <a href='{BASE_URL}/search' style='color:#aaa'>cbva.com/search</a></p>
    </body></html>"""


def build_changes_html(alerts: list) -> str:
    sections = ""
    for a in alerts:
        sections += _player_header(a["name"], a["profile_url"])

        if rc := a.get("rating_change"):
            arrow = "▲" if rc["increased"] else "▼"
            sections += (
                f"<p style='margin:.25em 0'>{arrow} Rating: "
                f"<strong>{rc['from']} → {rc['to']}</strong></p>"
            )

        for t in a.get("status_changes", []):
            sections += _CARD.format(
                color="#E8A020", bg="#fffaf0",
                title=f"Status update: {t['name']}",
                line2=f"{t['date']} · {t['location']}",
                line3=f"{t['division']} · {t['old_status']} → {t['status']} · Partner: {t['partner']}",
            )

        for t in a.get("new_tournaments", []):
            sections += _CARD.format(
                color="#1D9E75", bg="#f5faf8",
                title=f"New signup: {t['name']}",
                line2=f"{t['date']} · {t['location']}",
                line3=f"{t['division']} · {t['status']} · Partner: {t['partner']}",
            )

    return _wrap(sections, "CBVA Player Alert")


def build_today_html(today_entries: list) -> str:
    sections = ""
    for e in today_entries:
        t = e["tournament"]
        sections += _player_header(e["name"], e["profile_url"])
        sections += _CARD.format(
            color="#1a2a4a", bg="#f0f2f7",
            title=t["name"],
            line2=t["location"],
            line3=f"{t['division']} · Partner: {t['partner']}",
        )
    return _wrap(sections, "CBVA Playing Today")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    if not PLAYER_NAMES:
        print("PLAYER_NAMES is empty. Set the env var and try again.")
        return

    print(f"Monitoring {len(PLAYER_NAMES)} player(s): {', '.join(PLAYER_NAMES)}")
    state  = load_state()
    alerts = []
    today  = datetime.now().date()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        for name in PLAYER_NAMES:
            print(f"\nChecking: {name}")
            prev = state.get("players", {}).get(name, {})

            profile_url = prev.get("profile_url") or await find_profile_url(page, name)
            if not profile_url:
                print(f"  Skipping — profile not found.")
                continue

            debug_name = name.replace(" ", "_") if (DEBUG and name == PLAYER_NAMES[0]) else None
            cur = await scrape_profile(page, profile_url, debug_name=debug_name)
            print(f"  Rating: {cur['rating']}  Upcoming: {len(cur['upcoming_tournaments'])}")

            alert: dict = {
                "name":            name,
                "profile_url":     profile_url,
                "rating_change":   None,
                "status_changes":  [],
                "new_tournaments": [],
            }

            # Rating change
            if prev.get("rating") and prev["rating"] != cur["rating"] and cur["rating"] != "?":
                alert["rating_change"] = {
                    "from":      prev["rating"],
                    "to":        cur["rating"],
                    "increased": rating_rank(cur["rating"]) > rating_rank(prev["rating"]),
                }
                print(f"  Rating changed: {prev['rating']} -> {cur['rating']}")

            # New registrations and status changes
            prev_map = {tournament_key(t): t for t in prev.get("upcoming_tournaments", [])}
            for t in cur["upcoming_tournaments"]:
                key = tournament_key(t)
                if key not in prev_map:
                    alert["new_tournaments"].append(t)
                    print(f"  New signup: {t['name']} on {t['date']}")
                elif prev_map[key].get("status") != t.get("status") and t.get("status"):
                    alert["status_changes"].append({
                        **t,
                        "old_status": prev_map[key].get("status", ""),
                    })
                    print(f"  Status change: {t['name']}: {prev_map[key].get('status')} -> {t['status']}")

            if alert["rating_change"] or alert["new_tournaments"] or alert["status_changes"]:
                alerts.append(alert)

            state.setdefault("players", {})[name] = {
                **cur,
                "last_checked": datetime.now().isoformat(),
            }

        await browser.close()

    save_state(state)
    print(f"\nState saved to {STATE_FILE}")

    # ── Day-of tournament alerts ───────────────────────────────────────────────
    today_entries = []
    for name, data in state["players"].items():
        for t in data.get("upcoming_tournaments", []):
            d = parse_tournament_date(t.get("date", ""))
            if d and d.date() == today:
                today_entries.append({
                    "name":        name,
                    "profile_url": data.get("profile_url", ""),
                    "tournament":  t,
                })

    if today_entries:
        subject = f"CBVA Playing Today — {datetime.now().strftime('%b %d, %Y')}"
        send_email(subject, build_today_html(today_entries))
        print(f"Today alerts sent for {len(today_entries)} entry/entries.")

    # ── Change digest ─────────────────────────────────────────────────────────
    if alerts:
        subject = f"CBVA Alert — {datetime.now().strftime('%b %d, %Y')}"
        send_email(subject, build_changes_html(alerts))
        print(f"Change alerts sent for {len(alerts)} player(s).")
    else:
        print("No changes detected — no digest email sent.")


if __name__ == "__main__":
    asyncio.run(run())
