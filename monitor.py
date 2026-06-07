#!/usr/bin/env python3
"""
CBVA Player Monitor

Weekend runs (every 30 min via GitHub Actions):
  1. Check ratings once per day (first run of the day)
  2. Scan today's tournament rosters for watchlist players
  3. Send "Playing Today" alert on first discovery per player
  4. Check game results every run; email update when scores change

Weekday runs: exit immediately — CBVA tournaments are weekends only.

Required env / GitHub Secrets:
  EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO  — Gmail SMTP
  CBVA_EMAIL, CBVA_PASSWORD             — cbva.com login
  PLAYER_NAMES                          — comma-separated (loaded from gist)
Optional:
  CBVA_DEBUG=1                          — verbose API logging
"""

import asyncio
import json
import os
import re
import smtplib
import urllib.parse
from datetime import date, datetime, timezone, timedelta
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

# PDT = UTC-7 (summer).  Change to -8 (PST) Nov–Mar if needed.
_PT          = timezone(timedelta(hours=-7))
RATING_ORDER = ["N", "U", "B", "A", "AA", "AAA", "Open"]
_LEVEL_MAP   = {"unrated":"U","n":"N","u":"U","b":"B","a":"A","aa":"AA","aaa":"AAA","open":"Open"}


# ── State ─────────────────────────────────────────────────────────────────────

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
        print("[email] Credentials missing — printing instead.\n")
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
    if not CBVA_EMAIL or not CBVA_PASSWORD:
        print("[auth] No credentials — running unauthenticated.")
        return False
    try:
        await page.goto(BASE_URL, wait_until="networkidle")
        link = await page.query_selector("a[href*='login'], button:has-text('Log'), a:has-text('Log')")
        if link:
            await link.click()
            await page.wait_for_timeout(2_000)
        email_sel = "input[type='email'], input[name='email'], input[placeholder*='email' i]"
        await page.wait_for_selector(email_sel, timeout=10_000)
        await page.fill(email_sel, CBVA_EMAIL)
        await page.fill("input[type='password'], input[name='password']", CBVA_PASSWORD)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3_000)
        body = await page.evaluate("() => document.body.innerText")
        if "LOG OUT" in body.upper() or "SIGN OUT" in body.upper() or CBVA_EMAIL.split("@")[0].lower() in body.lower():
            print(f"[auth] Logged in as {CBVA_EMAIL}")
            return True
        if "login" not in page.url.lower():
            print(f"[auth] Logged in (URL: {page.url})")
            return True
        print("[auth] Login failed — check credentials.")
        return False
    except Exception as e:
        print(f"[auth] Error: {e}")
        return False


# ── tRPC ──────────────────────────────────────────────────────────────────────

async def _trpc_get(page, endpoint: str, input_obj: dict):
    enc = urllib.parse.quote(json.dumps({"json": input_obj}))
    raw = await page.evaluate(f"""
        async () => {{
            const r = await fetch('{BASE_URL}/api/trpc/{endpoint}?input={enc}');
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


# ── Profile helpers ───────────────────────────────────────────────────────────

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
        if all(p.lower() in text.lower() for p in name.split()):
            href = await link.get_attribute("href")
            print(f"  [search] {name} → {href}")
            return href
    links = await page.query_selector_all("a[href*='/profile/']")
    if links:
        href = await links[0].get_attribute("href")
        print(f"  [search] No exact match for '{name}', using {href}")
        return href
    print(f"  [search] No results for '{name}'")
    return None

def _profile_id(profile_url: str) -> int | None:
    try:
        return int(profile_url.rstrip("/").split("/")[-1])
    except (ValueError, AttributeError):
        return None


# ── Rating helpers ────────────────────────────────────────────────────────────

def parse_rating(overview: dict) -> str:
    try:
        level = overview.get("level") or {}
        raw   = (level.get("abbreviated") or level.get("name") or "").strip()
        return _LEVEL_MAP.get(raw.lower(), raw.upper()) if raw else "?"
    except Exception:
        return "?"

def parse_rank(overview: dict) -> str:
    try:
        return str(overview["rank"])
    except Exception:
        return "?"

def rating_rank(r: str) -> int:
    try:
        return RATING_ORDER.index(r)
    except ValueError:
        return -1


async def check_ratings(page, state: dict) -> list[dict]:
    """Fetch ratings for all watchlist players; return list of change alerts."""
    alerts = []
    for name in PLAYER_NAMES:
        print(f"  Rating check: {name}")
        prev        = state.get("players", {}).get(name, {})
        profile_url = prev.get("profile_url") or await find_profile_url(page, name)
        if not profile_url:
            print("    Skipping — profile not found.")
            continue
        pid = _profile_id(profile_url)
        if not pid:
            print(f"    Skipping — bad profile URL: {profile_url}")
            continue

        await page.goto(f"{BASE_URL}{profile_url}", wait_until="networkidle")
        overview = await _trpc_get(page, "profiles.getOverview", {"id": pid})
        if overview is None:
            print("    Skipping — no API response.")
            continue

        cur_rating = parse_rating(overview)
        cur_rank   = parse_rank(overview)
        print(f"    Rating: {cur_rating}  Rank: {cur_rank}")

        prev_rating = prev.get("rating", "")
        alert = {"name": name, "profile_url": profile_url, "rating_change": None}
        if prev_rating and prev_rating != cur_rating and cur_rating != "?":
            alert["rating_change"] = {
                "from":      prev_rating,
                "to":        cur_rating,
                "increased": rating_rank(cur_rating) > rating_rank(prev_rating),
            }
            print(f"    *** Rating changed: {prev_rating} → {cur_rating}")
            alerts.append(alert)

        state.setdefault("players", {})[name] = {
            **prev,
            "rating":       cur_rating,
            "rank":         cur_rank,
            "profile_url":  profile_url,
            "last_checked": datetime.now().isoformat(),
        }
    return alerts


# ── Tournament scanning ───────────────────────────────────────────────────────

def _full_name(player: dict) -> str:
    first = (player.get("preferredName") or player.get("firstName") or "").strip()
    last  = (player.get("lastName") or "").strip()
    return f"{first} {last}".strip()

def _names_match(api_name: str, watchlist_name: str) -> bool:
    a, w = api_name.strip().lower(), watchlist_name.strip().lower()
    if a == w:
        return True
    ap, wp = a.split(), w.split()
    # Last name + first two chars of first name
    if len(ap) >= 2 and len(wp) >= 2:
        return ap[-1] == wp[-1] and ap[0][:2] == wp[0][:2]
    return False

def _division_label(div: dict) -> str:
    try:
        inner   = div.get("division", {})
        gender  = div.get("gender", "male")
        level   = (inner.get("display") or inner.get("name") or inner.get("abbreviated") or "").upper()
        max_age = inner.get("maxAge") or div.get("maxAge")
        if gender == "coed":
            prefix = "Coed"
        elif max_age:
            prefix = "Boys" if gender == "male" else "Girls"
        else:
            prefix = "Men's" if gender == "male" else "Women's"
        return f"{prefix} {level}".strip()
    except Exception:
        return ""

def _extract_teams(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("teams", "entries", "registrations"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []

def _extract_players(team: dict) -> list[dict]:
    # Direct on the entry object
    for key in ("profiles", "players", "teammates", "members"):
        if key in team and isinstance(team[key], list):
            return team[key]
    # Nested under a "team" sub-object (getTeams returns registration entries)
    nested = team.get("team") or {}
    for key in ("profiles", "players", "teammates", "members"):
        if key in nested and isinstance(nested[key], list):
            return nested[key]
    return []


async def scan_today_tournaments(page, today_str: str, state: dict) -> list[dict]:
    """
    Scan published rosters for all of today's tournaments.
    Returns only newly discovered watchlist players (not previously notified).
    Updates state['tournament_day'] with playing entries.
    """
    td = state.get("tournament_day", {})
    if td.get("date") != today_str:
        state["tournament_day"] = {"date": today_str, "notified": [], "playing": {}}
        td = state["tournament_day"]

    notified = set(td.get("notified", []))

    # Build profile-ID → watchlist-name map for fast lookup
    id_to_wname: dict[int, str] = {}
    for wname in PLAYER_NAMES:
        url = state.get("players", {}).get(wname, {}).get("profile_url", "")
        pid = _profile_id(url)
        if pid:
            id_to_wname[pid] = wname

    # Navigate to tournaments page first to establish session context
    await page.goto(f"{BASE_URL}/tournaments", wait_until="networkidle")

    print(f"[scan] Searching tournaments for {today_str}")
    raw = await _trpc_get(page, "tournaments.search", {"date": today_str})
    if not raw:
        print("[scan] No tournaments returned.")
        return []

    tournaments = (raw if isinstance(raw, list)
                   else (raw.get("tournaments") or raw.get("data") or []))
    print(f"[scan] {len(tournaments)} tournament(s) today")

    new_entries: list[dict] = []

    for t in tournaments:
        t_id      = t.get("id")
        venue     = t.get("venue") or {}
        t_name    = t.get("name") or venue.get("name") or f"Tournament {t_id}"
        venue_str = ", ".join(filter(None, [venue.get("name"), venue.get("city")])) or "Unknown venue"

        await page.goto(f"{BASE_URL}/tournaments/{t_id}", wait_until="networkidle")
        details = await _trpc_get(page, "tournaments.get", {"id": t_id})
        if not details:
            continue

        divisions = details.get("tournamentDivisions") or details.get("divisions") or []
        n_pub = sum(1 for d in divisions if d.get("rosterPublished"))
        print(f"  {t_name} ({venue_str}): {len(divisions)} divisions, {n_pub} roster(s) published")

        for div in divisions:
            if not div.get("rosterPublished"):
                continue
            div_id    = div.get("id") or div.get("tournamentDivisionId")
            div_label = _division_label(div)

            await page.goto(f"{BASE_URL}/tournaments/{t_id}/{div_id}", wait_until="networkidle")
            teams_data = await _trpc_get(page, "tournaments.getTeams", {"tournamentDivisionId": div_id})
            if not teams_data:
                print(f"    [scan] getTeams({div_id}) returned nothing")
                continue

            teams = _extract_teams(teams_data)
            if DEBUG or True:  # temporary — remove once structure confirmed
                first = teams[0] if teams else {}
                players_sample = _extract_players(first)
                p0 = players_sample[0] if players_sample else {}
                print(f"    [scan] div {div_id} ({div_label}): {len(teams)} entries, "
                      f"player keys={list(p0.keys())[:15]}, "
                      f"sample={str(p0)[:300]}")

            for team in teams:
                players = _extract_players(team)
                for player in players:
                    p_id   = player.get("id") or player.get("profileId")
                    p_name = _full_name(player)

                    # Match by profile ID, fall back to name matching
                    wname = id_to_wname.get(p_id)
                    if not wname:
                        for candidate in PLAYER_NAMES:
                            if _names_match(p_name, candidate):
                                wname = candidate
                                break
                    if not wname:
                        continue

                    # Partner = other player on same team
                    partner, partner_id = "TBD", None
                    for other in players:
                        o_id = other.get("id") or other.get("profileId")
                        if o_id != p_id:
                            partner    = _full_name(other) or "TBD"
                            partner_id = o_id
                            break

                    entry = {
                        "player_name":     wname,
                        "profile_id":      p_id,
                        "tournament_id":   t_id,
                        "tournament_name": t_name,
                        "venue":           venue_str,
                        "division":        div_label,
                        "division_id":     div_id,
                        "partner":         partner,
                        "partner_id":      partner_id,
                    }
                    td["playing"][wname] = entry
                    print(f"    Found: {wname} · {div_label} · partner: {partner}")

                    if wname not in notified:
                        new_entries.append(entry)

    return new_entries


# ── Results tracking ──────────────────────────────────────────────────────────

# Candidate endpoints tried in order until one returns data.
# Add confirmed endpoint to the front once discovered on a real tournament day.
_RESULTS_CANDIDATES = [
    ("tournaments.getSchedule",      lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getGames",         lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getDivisionGames", lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getPoolPlay",      lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getBracket",       lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getDivision",      lambda d: {"id": d}),
    ("divisions.get",                lambda d: {"id": d}),
    ("divisions.getMatches",         lambda d: {"id": d}),
    ("pools.getForDivision",         lambda d: {"tournamentDivisionId": d}),
    ("pools.getByDivision",          lambda d: {"id": d}),
    ("matches.getByDivision",        lambda d: {"tournamentDivisionId": d}),
    ("games.list",                   lambda d: {"tournamentDivisionId": d}),
]

# Cached once we discover the working endpoint: div_id → endpoint name
_results_ep_cache: dict[int, str] = {}


async def fetch_results(page, div_id: int):
    if div_id in _results_ep_cache:
        ep = _results_ep_cache[div_id]
        return await _trpc_get(page, ep, {"tournamentDivisionId": div_id})

    for ep, inp_fn in _RESULTS_CANDIDATES:
        data = await _trpc_get(page, ep, inp_fn(div_id))
        if data and "error" not in str(data)[:50].lower():
            print(f"  [results] Endpoint discovered: {ep} (div {div_id})")
            _results_ep_cache[div_id] = ep
            return data
        if DEBUG:
            print(f"  [results] miss: {ep}")

    print(f"  [results] No endpoint found for div {div_id} — results not yet available.")
    return None


def _results_fingerprint(data) -> str:
    """Stable string for change detection — truncated JSON."""
    return json.dumps(data, sort_keys=True, default=str)[:3000]


async def check_results(page, state: dict) -> list[tuple[str, dict]]:
    """
    For each confirmed-playing watchlist player, fetch latest results.
    Returns list of (player_name, entry) where results changed since last run.
    """
    td = state.get("tournament_day", {})
    playing = td.get("playing", {})
    if not playing:
        return []

    results_state = td.setdefault("results", {})
    updates = []

    for wname, entry in playing.items():
        div_id = entry.get("division_id")
        if not div_id:
            continue

        await page.goto(
            f"{BASE_URL}/tournaments/{entry['tournament_id']}/{div_id}",
            wait_until="networkidle",
        )
        data = await fetch_results(page, div_id)
        if data is None:
            continue

        fp = _results_fingerprint(data)
        prev_fp = results_state.get(wname, {}).get("fingerprint", "")
        if fp != prev_fp:
            updates.append((wname, entry, data))
            results_state[wname] = {
                "fingerprint":    fp,
                "raw":            data,
                "last_updated":   datetime.now().isoformat(),
            }
            print(f"  [results] {wname}: results changed")

    return updates


# ── Email templates ───────────────────────────────────────────────────────────

_CARD = (
    "<div style='border-left:3px solid {color};padding:8px 14px;margin:6px 0;"
    "background:{bg};border-radius:0 6px 6px 0;font-size:14px'>"
    "<strong>{title}</strong><br>"
    "<span style='color:#555'>{line2}</span><br>"
    "<span style='color:#555'>{line3}</span>"
    "</div>"
)

def _player_header(name: str, profile_url: str) -> str:
    href = f"{BASE_URL}{profile_url}" if profile_url else "#"
    return (
        f"<h2 style='margin:1.5em 0 .4em;font-size:17px'>"
        f"<a href='{href}' style='color:#1a2a4a;text-decoration:none'>{name}</a></h2>"
    )

def _date_str(now_pt: datetime) -> str:
    return f"{now_pt.strftime('%b')} {now_pt.day}, {now_pt.year}"

def _wrap(body: str, title: str, now_pt: datetime) -> str:
    return (
        "<html><body style='font-family:sans-serif;max-width:600px;margin:0 auto;"
        "padding:24px;color:#222'>"
        f"<h1 style='font-size:20px;border-bottom:2px solid #1a2a4a;"
        f"padding-bottom:8px;margin-bottom:0'>{title} — {_date_str(now_pt)}</h1>"
        f"{body}"
        "<hr style='margin-top:2em;border:none;border-top:1px solid #ddd'>"
        f"<p style='font-size:11px;color:#aaa'>cbva-monitor · "
        f"<a href='{BASE_URL}/search' style='color:#aaa'>cbva.com</a></p>"
        "</body></html>"
    )


def build_rating_email(alerts: list[dict], now_pt: datetime) -> str:
    body = ""
    for a in alerts:
        body += _player_header(a["name"], a.get("profile_url", ""))
        if rc := a.get("rating_change"):
            arrow = "▲" if rc["increased"] else "▼"
            body += f"<p style='margin:.25em 0'>{arrow} Rating: <strong>{rc['from']} → {rc['to']}</strong></p>"
    return _wrap(body, "CBVA Rating Alert", now_pt)


def build_playing_today_email(entries: list[dict], state: dict, now_pt: datetime) -> str:
    body = ""
    for e in entries:
        profile_url = state.get("players", {}).get(e["player_name"], {}).get("profile_url", "")
        t_url = f"{BASE_URL}/tournaments/{e['tournament_id']}/{e['division_id']}"
        body += _player_header(e["player_name"], profile_url)
        body += _CARD.format(
            color="#1a2a4a", bg="#f0f2f7",
            title=f"<a href='{t_url}' style='color:#1a2a4a'>{e['tournament_name']}</a>",
            line2=e["venue"],
            line3=f"{e['division']} · Partner: {e['partner']}",
        )
    return _wrap(body, "CBVA Playing Today", now_pt)


def _format_results_body(data) -> str:
    """Best-effort HTML for unknown result structure — improved once endpoint is known."""
    if not data:
        return "<p style='color:#888'>No result data.</p>"
    if isinstance(data, list):
        rows = "".join(
            f"<li style='margin:.3em 0;font-size:13px'>{json.dumps(item, default=str)[:300]}</li>"
            for item in data[:10]
        )
        return f"<ul style='padding-left:1.5em'>{rows}</ul>"
    if isinstance(data, dict):
        lines = []
        for key in ("pools", "bracket", "matches", "games", "schedule", "results"):
            if key in data:
                lines.append(
                    f"<p style='margin:.4em 0'><b>{key.capitalize()}</b>: "
                    f"{json.dumps(data[key], default=str)[:400]}</p>"
                )
        if lines:
            return "\n".join(lines)
    return f"<pre style='font-size:12px;overflow:auto'>{json.dumps(data, default=str)[:800]}</pre>"


def build_results_email(updates: list[tuple], state: dict, now_pt: datetime) -> str:
    body = ""
    for wname, entry, raw_data in updates:
        profile_url = state.get("players", {}).get(wname, {}).get("profile_url", "")
        t_url = f"{BASE_URL}/tournaments/{entry.get('tournament_id', '')}/{entry.get('division_id', '')}"
        body += _player_header(wname, profile_url)
        body += (
            f"<p style='margin:.3em 0;font-size:13px;color:#555'>"
            f"{entry.get('tournament_name','')} · {entry.get('venue','')} · {entry.get('division','')}"
            f" · <a href='{t_url}'>view bracket</a></p>"
        )
        body += _format_results_body(raw_data)
    return _wrap(body, "CBVA Results Update", now_pt)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    if not PLAYER_NAMES:
        print("PLAYER_NAMES is empty — set the env var and try again.")
        return

    now_pt   = datetime.now(_PT)
    today_pt = now_pt.date()

    if today_pt.weekday() < 5:  # 0=Mon … 4=Fri; 5=Sat, 6=Sun
        print(f"Today is {today_pt.strftime('%A')} in PT — no tournaments on weekdays. Exiting.")
        return

    today_str = today_pt.strftime("%Y-%m-%d")
    print(f"CBVA monitor — {today_pt.strftime('%A, %B')} {today_pt.day}, {today_pt.year} PT")
    print(f"Watching {len(PLAYER_NAMES)} player(s): {', '.join(PLAYER_NAMES)}")

    state = load_state()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()
        await login(page)

        # ── Ratings (once per day, first run only) ─────────────────────────
        if state.get("last_rating_check") != today_str:
            print("\n── Rating checks ─────────────────────────────────────────")
            rating_alerts = await check_ratings(page, state)
            state["last_rating_check"] = today_str
            if rating_alerts:
                names_str = ", ".join(a["name"] for a in rating_alerts)
                send_email(
                    f"CBVA Rating Alert — {_date_str(now_pt)}: {names_str}",
                    build_rating_email(rating_alerts, now_pt),
                )
        else:
            print("\n[ratings] Already checked today — skipping.")

        # ── Tournament roster scan ─────────────────────────────────────────
        print("\n── Tournament scan ───────────────────────────────────────────")
        new_entries = await scan_today_tournaments(page, today_str, state)

        if new_entries:
            names_playing = [e["player_name"] for e in new_entries]
            print(f"\nPlaying today (new): {', '.join(names_playing)}")
            send_email(
                f"CBVA Playing Today — {_date_str(now_pt)}: {', '.join(names_playing)}",
                build_playing_today_email(new_entries, state, now_pt),
            )
            state["tournament_day"]["notified"].extend(names_playing)
        else:
            n_total = len(state.get("tournament_day", {}).get("playing", {}))
            n_notified = len(state.get("tournament_day", {}).get("notified", []))
            print(f"\n[scan] No new players found. "
                  f"{n_total} on rosters total, {n_notified} already notified.")

        # ── Results updates ────────────────────────────────────────────────
        playing = state.get("tournament_day", {}).get("playing", {})
        if playing:
            print(f"\n── Results check ({len(playing)} player(s)) ──────────────────")
            result_updates = await check_results(page, state)
            if result_updates:
                send_email(
                    f"CBVA Results Update — {now_pt.strftime('%b')} {now_pt.day} "
                    f"{now_pt.strftime('%I:%M %p')} PT",
                    build_results_email(result_updates, state, now_pt),
                )
                print(f"Results update sent for {len(result_updates)} player(s).")
            else:
                print("[results] No changes since last run.")
        else:
            print("\n[results] No confirmed players yet — skipping results check.")

        await browser.close()

    save_state(state)
    print("\nState saved.")


if __name__ == "__main__":
    asyncio.run(run())
