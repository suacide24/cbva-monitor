#!/usr/bin/env python3
"""
CBVA Player Monitor
Uses CBVA's tRPC API to detect new tournament registrations, rating changes,
and status updates. Sends email alerts for changes + day-of tournament notices.

Required secrets (GitHub Actions or local .env):
  EMAIL_FROM      — Gmail address to send from
  EMAIL_PASSWORD  — Gmail App Password
  EMAIL_TO        — recipient address (defaults to EMAIL_FROM)
  CBVA_EMAIL      — your cbva.com login email
  CBVA_PASSWORD   — your cbva.com login password

Optional:
  PLAYER_NAMES    — comma/newline list (loaded from gist in Actions)
  CBVA_DEBUG      — set to "1" to enable verbose API logging
"""

import asyncio
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

PLAYER_NAMES  = [n.strip() for n in re.split(r"[,\n]", os.environ.get("PLAYER_NAMES", "")) if n.strip()]
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")
EMAIL_TO      = os.environ.get("EMAIL_TO") or EMAIL_FROM
EMAIL_PASS    = os.environ.get("EMAIL_PASSWORD", "")
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")
STATE_FILE    = "state.json"
BASE_URL      = "https://cbva.com"
DEBUG         = os.environ.get("CBVA_DEBUG") == "1"

RATING_ORDER  = ["N", "U", "B", "A", "AA", "AAA", "Open"]


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


# ── CBVA auth ─────────────────────────────────────────────────────────────────

async def login(page) -> bool:
    """Log in to CBVA. Returns True on success."""
    if not CBVA_EMAIL or not CBVA_PASSWORD:
        print("[auth] No CBVA credentials — running unauthenticated (registrations unavailable).")
        return False
    try:
        await page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        await page.fill("input[type='email']", CBVA_EMAIL)
        await page.fill("input[type='password']", CBVA_PASSWORD)
        await page.click("button[type='submit']")
        await page.wait_for_timeout(3_000)
        if "/login" not in page.url:
            print(f"[auth] Logged in as {CBVA_EMAIL}")
            return True
        print("[auth] Login failed — check CBVA_EMAIL / CBVA_PASSWORD secrets.")
        return False
    except Exception as e:
        print(f"[auth] Login error: {e}")
        return False


# ── CBVA API helpers ──────────────────────────────────────────────────────────

async def _trpc_get(page, endpoint: str, input_obj: dict) -> dict | None:
    """Call a single tRPC endpoint via browser fetch and return parsed JSON."""
    input_enc = json.dumps({"json": input_obj}).replace('"', '\\"')
    raw = await page.evaluate(f"""
        async () => {{
            const input = encodeURIComponent('{input_enc}');
            const r = await fetch('{BASE_URL}/api/trpc/{endpoint}?input=' + input);
            const reader = r.body.getReader();
            const dec = new TextDecoder();
            let out = '';
            while (true) {{
                const {{done, value}} = await reader.read();
                if (done) break;
                out += dec.decode(value, {{stream: true}});
            }}
            return out;
        }}
    """)
    if DEBUG:
        print(f"  [api] {endpoint}: {raw[:400]}")
    try:
        return json.loads(raw).get("result", {}).get("data", {}).get("json")
    except Exception:
        return None


async def get_overview(page, profile_id: int) -> dict | None:
    return await _trpc_get(page, "profiles.getOverview", {"id": profile_id})


async def get_registrations(page, profile_id: int) -> list[dict]:
    data = await _trpc_get(page, "profiles.getRegistrations", {"profileId": profile_id})
    if not data:
        return []
    if not data.get("authorized", True):
        return []
    return data.get("registrations", [])


# ── Profile search ────────────────────────────────────────────────────────────

async def find_profile_url(page, name: str) -> str | None:
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


# ── Data parsers ──────────────────────────────────────────────────────────────

def parse_rating(overview: dict) -> str:
    try:
        return overview["level"]["abbreviated"].upper()
    except Exception:
        return "?"

def parse_rank(overview: dict) -> str:
    try:
        return str(overview["rank"])
    except Exception:
        return "?"

def _format_gender(gender: str, max_age: int | None) -> str:
    if gender == "coed":
        return "Coed"
    prefix = "Boy's" if max_age else ("Men's" if gender == "male" else "Women's")
    return prefix

def _format_division(reg: dict) -> str:
    try:
        td = reg["tournamentDivision"]
        name = td.get("name") or ""
        gender = _format_gender(td.get("gender", "male"), td.get("division", {}).get("maxAge"))
        level  = td.get("division", {}).get("display") or td.get("division", {}).get("name", "")
        return f"{gender} {level}".strip() if not name else name
    except Exception:
        return ""

def _format_date(date_str: str) -> str:
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return date_str

def _format_status(reg: dict) -> str:
    status = reg.get("status", "")
    waitlist_pos = reg.get("waitlistPosition")
    if status == "waitlisted" and waitlist_pos:
        return f"Waitlisted #{waitlist_pos}"
    return status.capitalize() if status else ""

def _format_partner(reg: dict) -> str:
    try:
        partner = reg.get("partner") or {}
        return f"{partner.get('firstName','')} {partner.get('lastName','')}".strip() or "TBD"
    except Exception:
        return "TBD"

def parse_registrations(raw_regs: list[dict]) -> list[dict]:
    tournaments = []
    for reg in raw_regs:
        try:
            t = reg.get("tournament", {})
            venue = t.get("venue", {})
            tournaments.append({
                "name":     t.get("name", ""),
                "date":     _format_date(t.get("date", "")),
                "location": f"{venue.get('name','')}, {venue.get('city','')}".strip(", "),
                "status":   _format_status(reg),
                "division": _format_division(reg),
                "partner":  _format_partner(reg),
            })
        except Exception as ex:
            if DEBUG:
                print(f"  [parse] Registration parse error: {ex} — {reg}")
    return tournaments


# ── Change detection ──────────────────────────────────────────────────────────

def rating_rank(r: str) -> int:
    try:
        return RATING_ORDER.index(r)
    except ValueError:
        return -1

def tournament_key(t: dict) -> str:
    return f"{t.get('name')}-{t.get('date')}-{t.get('division')}"

def parse_tournament_date(date_str: str) -> datetime | None:
    for fmt in ("%b %d, %Y", "%b %-d, %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
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
      {{body}}
      <hr style='margin-top:2em;border:none;border-top:1px solid #ddd'>
      <p style='font-size:11px;color:#aaa'>cbva-monitor · <a href='{BASE_URL}/search' style='color:#aaa'>cbva.com/search</a></p>
    </body></html>""".format(body=body)


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
    authenticated = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        authenticated = await login(page)

        for name in PLAYER_NAMES:
            print(f"\nChecking: {name}")
            prev = state.get("players", {}).get(name, {})

            profile_url = prev.get("profile_url") or await find_profile_url(page, name)
            if not profile_url:
                print(f"  Skipping — profile not found.")
                continue

            # Extract numeric profile ID
            try:
                profile_id = int(profile_url.rstrip("/").split("/")[-1])
            except ValueError:
                print(f"  Skipping — cannot parse profile ID from {profile_url}")
                continue

            # Navigate to profile so cookies/session apply to fetch calls
            await page.goto(f"{BASE_URL}{profile_url}", wait_until="networkidle")

            # Fetch data via tRPC API
            overview = await get_overview(page, profile_id)
            raw_regs = await get_registrations(page, profile_id)

            if overview is None:
                print(f"  Skipping — could not fetch overview data.")
                continue

            cur_rating = parse_rating(overview)
            cur_rank   = parse_rank(overview)
            cur_tournaments = parse_registrations(raw_regs)

            print(f"  Rating: {cur_rating}  Rank: {cur_rank}  Upcoming: {len(cur_tournaments)}")
            if cur_tournaments:
                for t in cur_tournaments:
                    print(f"    - {t['name']} on {t['date']} ({t['status']})")

            alert: dict = {
                "name":            name,
                "profile_url":     profile_url,
                "rating_change":   None,
                "status_changes":  [],
                "new_tournaments": [],
            }

            # Rating change
            prev_rating = prev.get("rating", "")
            if prev_rating and prev_rating != cur_rating and cur_rating != "?":
                alert["rating_change"] = {
                    "from":      prev_rating,
                    "to":        cur_rating,
                    "increased": rating_rank(cur_rating) > rating_rank(prev_rating),
                }
                print(f"  Rating changed: {prev_rating} -> {cur_rating}")

            # New signups and status changes
            prev_map = {tournament_key(t): t for t in prev.get("upcoming_tournaments", [])}
            for t in cur_tournaments:
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
                "rating":               cur_rating,
                "rank":                 cur_rank,
                "upcoming_tournaments": cur_tournaments,
                "profile_url":          profile_url,
                "last_checked":         datetime.now().isoformat(),
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
        subject = f"CBVA Playing Today — {datetime.now().strftime('%b %-d, %Y')}"
        send_email(subject, build_today_html(today_entries))
        print(f"Today alerts sent for {len(today_entries)} entry/entries.")

    # ── Change digest ─────────────────────────────────────────────────────────
    if alerts:
        subject = f"CBVA Alert — {datetime.now().strftime('%b %-d, %Y')}"
        send_email(subject, build_changes_html(alerts))
        print(f"Change alerts sent for {len(alerts)} player(s).")
    else:
        print("No changes detected — no digest email sent.")

    if not authenticated:
        print("\nNOTE: Running without CBVA auth — tournament registrations not available.")
        print("Add CBVA_EMAIL and CBVA_PASSWORD secrets to enable full tracking.")


if __name__ == "__main__":
    asyncio.run(run())
