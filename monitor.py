#!/usr/bin/env python3
from __future__ import annotations
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

EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")
EMAIL_TO      = os.environ.get("EMAIL_TO") or EMAIL_FROM  # fallback only
EMAIL_PASS    = os.environ.get("EMAIL_PASSWORD", "")
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")
STATE_FILE    = "state.json"
PLAYERS_FILE  = "players.json"
STATUS_FILE   = "status.json"   # per-run heartbeat; NOT committed (gitignored)
BASE_URL      = "https://cbva.com"
DEBUG         = os.environ.get("CBVA_DEBUG") == "1"

# PDT = UTC-7 (summer).  Change to -8 (PST) Nov–Mar if needed.
_PT          = timezone(timedelta(hours=-7))
RATING_ORDER = ["N", "U", "B", "A", "AA", "AAA", "Open"]
_LEVEL_MAP   = {"unrated":"U","n":"N","u":"U","b":"B","a":"A","aa":"AA","aaa":"AAA","open":"Open"}

# ── Run-scoped diagnostics (reset each run()) ─────────────────────────────────
_run_errors:      list[dict] = []   # errors accumulated this run
_trpc_responses:  dict[str, str] = {}  # endpoint → last raw response text


# ── State ─────────────────────────────────────────────────────────────────────

USERS_FILE = "users.json"

def load_users() -> list[dict]:
    """Load users.json — list of {email, players} dicts."""
    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
        return [u for u in users if u.get("email") and u.get("players")]
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[users] Failed to load {USERS_FILE}: {e}")
        return []


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

def send_email(subject: str, html: str, to: str = "") -> None:
    recipient = to or EMAIL_TO
    if not all([EMAIL_FROM, recipient, EMAIL_PASS]):
        print("[email] Credentials missing — printing instead.\n")
        print(f"To: {recipient}\nSubject: {subject}\n{html}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = recipient
    msg.attach(MIMEText(html, "html"))
    import time
    for attempt in range(3):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(EMAIL_FROM, EMAIL_PASS)
                s.sendmail(EMAIL_FROM, recipient, msg.as_string())
            print(f"[email] Sent: {subject}")
            return
        except Exception as e:
            print(f"[email] Attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(5)
    print(f"[email] All 3 attempts failed for: {subject}")


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
    _trpc_responses[endpoint] = raw[:4000]  # keep for diagnostics
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


async def check_ratings(page, state: dict, player_names: list[str]) -> list[dict]:
    """Fetch ratings for all watchlist players; return list of change alerts."""
    alerts = []
    for name in player_names:
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
    # Name may be at root level (old format) or nested under "profile" (new format)
    p     = player.get("profile") or player
    first = (p.get("preferredName") or p.get("firstName") or "").strip()
    last  = (p.get("lastName") or "").strip()
    return f"{first} {last}".strip()

def _player_profile_id(player: dict) -> int | None:
    """Extract the CBVA profile ID regardless of where it lives in the object."""
    # New format: junction record with playerProfileId + nested profile.id
    for key in ("playerProfileId", "profileId"):
        v = player.get(key)
        if v:
            return int(v)
    nested = player.get("profile") or {}
    v = nested.get("id")
    if v:
        return int(v)
    # Fallback: bare id (old format where player IS the profile)
    v = player.get("id")
    return int(v) if v else None

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


async def scan_today_tournaments(page, today_str: str, state: dict, player_names: list[str]) -> list[dict]:
    """
    Scan published rosters for all of today's tournaments.
    Returns all discovered watchlist players found on rosters today.
    Updates state['tournament_day'] with playing entries.
    """
    td = state.get("tournament_day", {})
    if td.get("date") != today_str:
        state["tournament_day"] = {"date": today_str, "notified": {}, "playing": {}}
        td = state["tournament_day"]

    # Build profile-ID → watchlist-name map for fast lookup
    id_to_wname: dict[int, str] = {}
    for wname in player_names:
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
            if DEBUG:
                first = teams[0] if teams else {}
                p0 = (_extract_players(first) or [{}])[0]
                print(f"    [scan] div {div_id} ({div_label}): {len(teams)} entries, "
                      f"sample player={str(p0)[:200]}")

            # Store seed→names for every team so results emails can show opponent names
            roster_map: dict[int, str] = {}
            for team in teams:
                seed = team.get("seed")
                if seed:
                    names = [_full_name(p) for p in _extract_players(team) if _full_name(p)]
                    if names:
                        roster_map[seed] = " / ".join(names)
            if roster_map:
                td.setdefault("div_rosters", {})[div_id] = roster_map

            for team in teams:
                players = _extract_players(team)
                for player in players:
                    p_id   = _player_profile_id(player)
                    p_name = _full_name(player)

                    # Match by profile ID, fall back to name matching
                    wname = id_to_wname.get(p_id)
                    if not wname:
                        for candidate in player_names:
                            if _names_match(p_name, candidate):
                                wname = candidate
                                break
                    if not wname:
                        continue

                    # Partner = other player on same team
                    partner, partner_id = "TBD", None
                    for other in players:
                        o_id = _player_profile_id(other)
                        if o_id != p_id:
                            partner    = _full_name(other) or "TBD"
                            partner_id = o_id
                            break

                    # Capture team seed for bracket matching in results
                    team_seed = team.get("seed")

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
                        "team_seed":       team_seed,
                        "team_id":         team.get("teamId"),
                    }
                    if wname not in td["playing"]:
                        new_entries.append(entry)
                    td["playing"][wname] = entry
                    print(f"    Found: {wname} · {div_label} · partner: {partner}")

    return new_entries


# ── Results tracking ──────────────────────────────────────────────────────────

# Results are read straight from the known CBVA tRPC endpoints (getTeams +
# getPools + getPlayoffs) in _fetch_division_results / check_results below.

# ══════════════════════════════════════════════════════════════════════════════
# Tournament Availability Tracker
#
# Feature 1 — City watch:   notify when a new tournament matching city +
#             optional gender/division filters is listed on CBVA.
# Feature 2 — URL watch:    notify when a specific tournament URL's
#             registration status improves (e.g. Waitlist Full → Register).
#
# Config lives in users.json per-user:
#   "tournament_watches": [{"city": "San Diego", "genders": ["Men's"],
#                           "divisions": ["A", "AA"]}]
#   "tournament_urls":    [{"url": "https://cbva.com/tournaments/123/456", "nickname": "OB Men's A"}]
#                         (string form also accepted for backward compat)
#
# State lives in state.json under "tournament_tracker".
# ══════════════════════════════════════════════════════════════════════════════

_LEVEL_NORM: dict[str, str] = {
    "": "U", "n": "U", "u": "U", "unrated": "U",
    "b": "B", "a": "A", "aa": "AA", "aaa": "AAA",
    "open": "OPEN", "o": "OPEN",
}


def _norm_div_level(raw: str) -> str:
    return _LEVEL_NORM.get(raw.strip().lower(), raw.strip().upper())


def _div_gender_label(div: dict) -> str:
    g       = (div.get("gender") or "male").lower()
    max_age = (div.get("division") or {}).get("maxAge") or div.get("maxAge")
    if g == "coed":
        return "Coed"
    if max_age:
        return "Boys" if g == "male" else "Girls"
    return "Men's" if g == "male" else "Women's"


def _div_level_label(div: dict) -> str:
    inner = div.get("division") or {}
    raw   = inner.get("display") or inner.get("name") or inner.get("abbreviated") or ""
    return _norm_div_level(raw)


def _div_matches_watch(div: dict, watch: dict) -> bool:
    """Return True if a tournament division satisfies a city-watch filter."""
    want_genders = {g.strip() for g in (watch.get("genders") or [])}
    want_levels  = {_norm_div_level(d) for d in (watch.get("divisions") or [])}
    if want_genders and _div_gender_label(div) not in want_genders:
        return False
    if want_levels and _div_level_label(div) not in want_levels:
        return False
    return True


# Higher rank = better for the subscriber
_REG_RANK: dict[str, int] = {
    "unknown":       0,
    "coming_soon":   1,
    "closed":        2,
    "waitlist_full": 3,
    "waitlist":      4,
    "open":          5,
}
_REG_LABEL: dict[str, str] = {
    "open":          "Register",
    "waitlist":      "Join Waitlist",
    "waitlist_full": "Waitlist Full",
    "closed":        "Registration Closed",
    "coming_soon":   "Coming Soon",
    "unknown":       "Unknown",
}


def _parse_reg_status(page_text: str) -> str:
    """
    Fallback: infer registration status from CBVA page body text (button labels).

    Fragile — CBVA has renamed these buttons before. Prefer
    `_reg_status_from_division` (capacity numbers, label-independent); this is
    only used when the API division data is unavailable.
    """
    u = page_text.upper()
    if "WAITLIST FULL" in u or "WAITLIST IS FULL" in u:
        return "waitlist_full"
    if "JOIN WAITLIST" in u:
        return "waitlist"
    if "REGISTER" in u:
        return "open"
    if "COMING SOON" in u:
        return "coming_soon"
    if "REGISTRATION CLOSED" in u:
        return "closed"
    return "unknown"


def _parse_iso(s) -> datetime | None:
    """Parse an ISO-8601 string (with or without trailing Z / tz) to aware UTC."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _reg_status_from_division(div: dict, reg_open_at: datetime | None,
                              now_utc: datetime | None) -> str | None:
    """
    Derive registration status from a CBVA division's capacity numbers rather
    than scraping button text. Returns a status key, or None when the data is
    insufficient (caller should fall back to `_parse_reg_status`).

    Mapping (validated against live CBVA pages, 2026-06):
      now < registrationOpenAt                      -> coming_soon
      registrationPaused or division running/done   -> closed
      confirmedCount < capacity                     -> open
      main full, waitlistedCount < waitlistCapacity -> waitlist
      main full, waitlist full                      -> waitlist_full

    The division-level `status` field ("closed"/"running"/…) is NOT the
    registration button state and is only consulted to detect a tournament
    that is already underway or finished.
    """
    cap  = div.get("capacity")
    conf = div.get("confirmedCount")
    if cap is None or conf is None:
        return None  # insufficient data → let the caller fall back to text

    # Not yet open for registration
    if reg_open_at is not None and now_utc is not None and now_utc < reg_open_at:
        return "coming_soon"

    # Registration paused, or the tournament is already underway / finished
    dstatus = (div.get("status") or "").lower()
    if div.get("registrationPaused") or dstatus in ("running", "in_progress", "completed", "complete"):
        return "closed"

    if conf < cap:
        return "open"

    wcap = div.get("waitlistCapacity")
    wl   = div.get("waitlistedCount")
    if wcap is None or wl is None:
        return "waitlist_full"          # main roster full, no waitlist info
    return "waitlist" if wl < wcap else "waitlist_full"


def _url_entry_url(entry) -> str:
    """Extract the URL string from a tournament_urls entry (str or {url, nickname})."""
    return entry["url"] if isinstance(entry, dict) else entry


def _t_date_str(obj: dict) -> str:
    """Extract a human-readable date string from a tournament or details dict."""
    for field in ("date", "startDate", "scheduledDate", "tournamentDate"):
        raw = obj.get(field)
        if raw:
            try:
                return datetime.fromisoformat(
                    str(raw).replace("Z", "+00:00")
                ).strftime("%B %-d, %Y")
            except Exception:
                return str(raw)[:10]
    return ""


async def _fetch_upcoming_tournaments(page, weeks: int = 8) -> list[dict]:
    """
    Return deduplicated upcoming CBVA tournament objects.
    Tries a dedicated tRPC endpoint first; falls back to scanning the next
    `weeks` weekends via tournaments.search (one call per weekend day).
    """
    await page.goto(f"{BASE_URL}/tournaments", wait_until="networkidle")

    for ep in ("tournaments.getUpcoming", "tournaments.getAll", "tournaments.getFuture"):
        data = await _trpc_get(page, ep, {})
        if isinstance(data, list) and data:
            print(f"  [avail] {len(data)} tournament(s) via {ep}")
            return data

    # Fallback: scan upcoming Saturdays and Sundays
    today = date.today()
    seen:  dict[int, dict] = {}
    d = today
    for _ in range(weeks * 7):
        if d.weekday() in (5, 6):
            ds  = d.strftime("%Y-%m-%d")
            raw = await _trpc_get(page, "tournaments.search", {"date": ds})
            ts  = (raw if isinstance(raw, list)
                   else (raw or {}).get("tournaments") or (raw or {}).get("data") or []
                   if isinstance(raw, dict) else [])
            for t in ts:
                t_id = t.get("id")
                if t_id and t_id not in seen:
                    seen[t_id] = t
        d += timedelta(days=1)

    print(f"  [avail] {len(seen)} tournament(s) via {weeks}-week weekend scan")
    return list(seen.values())


async def check_city_tournaments(page, state: dict, users: list[dict], now_pt: datetime) -> None:
    """
    Phase 0a — City tournament watch.
    Fetches upcoming tournaments once per day, filters each user's city +
    gender + division preferences, and emails on newly seen tournament-divisions.
    """
    watching_users = [
        (u, w)
        for u in users
        for w in (u.get("tournament_watches") or [])
        if w.get("city")
    ]
    if not watching_users:
        return

    tt        = state.setdefault("tournament_tracker", {})
    today_str = date.today().strftime("%Y-%m-%d")

    if tt.get("city_last_checked") == today_str:
        print("[avail] City watch already checked today — skipping.")
        return

    print("\n── Tournament Availability: City Watches ─────────────────────")
    upcoming = await _fetch_upcoming_tournaments(page)
    tt["city_last_checked"] = today_str

    if not upcoming:
        print("[avail] No upcoming tournaments found.")
        return

    # Index by lowercase city for fast lookup
    wanted_cities = {w.get("city", "").strip().lower() for _, w in watching_users}
    by_city: dict[str, list[dict]] = {}
    for t in upcoming:
        city = ((t.get("venue") or {}).get("city") or "").strip().lower()
        if city in wanted_cities:
            by_city.setdefault(city, []).append(t)

    # Cache per-tournament details to avoid redundant API calls
    detail_cache: dict[int, dict | None] = {}

    async def _get_details(t_id: int) -> dict | None:
        if t_id not in detail_cache:
            await page.goto(f"{BASE_URL}/tournaments/{t_id}", wait_until="networkidle")
            detail_cache[t_id] = await _trpc_get(page, "tournaments.get", {"id": t_id})
        return detail_cache[t_id]

    city_ws = tt.setdefault("city_watches", {})

    for user in users:
        watches = [w for w in (user.get("tournament_watches") or []) if w.get("city")]
        if not watches:
            continue

        email    = user["email"]
        user_new: list[dict] = []

        for watch in watches:
            city_key  = watch["city"].strip().lower()
            state_key = f"{email}:{city_key}"
            ws        = city_ws.setdefault(state_key, {"seen_tdivs": []})
            seen      = set(ws["seen_tdivs"])

            for t in by_city.get(city_key, []):
                t_id    = t.get("id")
                details = await _get_details(t_id)
                if not details:
                    continue

                divs     = details.get("tournamentDivisions") or details.get("divisions") or []
                new_divs = []
                for div in divs:
                    if not _div_matches_watch(div, watch):
                        continue
                    div_id = div.get("id") or div.get("tournamentDivisionId")
                    key    = f"{t_id}:{div_id}"
                    if key not in seen:
                        new_divs.append(div)
                        seen.add(key)

                if new_divs:
                    venue     = t.get("venue") or details.get("venue") or {}
                    t_name    = (t.get("name") or details.get("name")
                                 or venue.get("name") or f"Tournament {t_id}")
                    venue_str = ", ".join(filter(None, [venue.get("name"), venue.get("city")]))
                    t_date    = _t_date_str(details) or _t_date_str(t)
                    user_new.append({
                        "t_id":         t_id,
                        "t_name":       t_name,
                        "venue_str":    venue_str,
                        "t_date":       t_date,
                        "t_date_short": _t_date_short(details) or _t_date_short(t),
                        "t_url":        f"{BASE_URL}/tournaments/{t_id}",
                        "watch":        watch,
                        "new_divs":     new_divs,
                    })

            ws["seen_tdivs"] = sorted(seen)

        if user_new:
            cities = ", ".join(dict.fromkeys(r["watch"]["city"] for r in user_new))
            n_t    = len(user_new)
            n_d    = sum(len(r["new_divs"]) for r in user_new)
            send_email(
                _new_tournament_subject(user_new),
                _build_new_tournament_email(user_new, now_pt),
                to=email,
            )
            print(f"[avail] City email → {email}: {n_t} tournament(s), {n_d} division(s)")


async def check_url_statuses(page, state: dict, users: list[dict], now_pt: datetime) -> None:
    """
    Phase 0b — Tournament URL registration-status watch.
    Navigates to each watched URL, reads the registration button text, and
    emails users when the status improves (Waitlist Full → Join Waitlist → Register).
    """
    all_urls = {_url_entry_url(e) for u in users for e in (u.get("tournament_urls") or [])}
    if not all_urls:
        return

    print("\n── Tournament Availability: URL Watches ──────────────────────")
    tt      = state.setdefault("tournament_tracker", {})
    url_ws  = tt.setdefault("url_watches", {})
    now_utc = datetime.now(timezone.utc)

    # Bound growth: forget watches no one is tracking anymore.
    stale = [u for u in url_ws if u not in all_urls]
    for u in stale:
        del url_ws[u]
    if stale:
        print(f"  [avail] Pruned {len(stale)} unwatched URL(s) from state")

    # tRPC fetches run in the page context and must be same-origin, so make sure
    # we're on a cbva.com page first (one navigation total, not one per URL).
    if "cbva.com" not in (page.url or ""):
        try:
            await page.goto(f"{BASE_URL}/tournaments", wait_until="domcontentloaded")
        except Exception as exc:
            print(f"  [avail] Could not reach CBVA: {exc}")
            return

    for url in sorted(all_urls):
        m = re.search(r"/tournaments/(\d+)(?:/(\d+))?", url)
        if not m:
            print(f"  [avail] Unrecognised tournament URL — skipping: {url}")
            continue
        t_id   = int(m.group(1))
        div_id = int(m.group(2)) if m.group(2) else None

        # Primary source of truth: the tRPC division data (capacity numbers).
        # This also supplies the metadata for the notification email, so the
        # common path needs no page navigation at all.
        try:
            details = await _trpc_get(page, "tournaments.get", {"id": t_id})
        except Exception as exc:
            print(f"  [avail] Error fetching tournament {t_id}: {exc}")
            continue

        t_name    = ""
        div_name  = ""
        venue_str = ""
        t_date    = ""
        div_obj   = None
        if details:
            venue     = details.get("venue") or {}
            t_name    = details.get("name") or venue.get("name") or f"Tournament {t_id}"
            venue_str = ", ".join(filter(None, [venue.get("name"), venue.get("city")]))
            t_date    = _t_date_str(details)
            if div_id:
                for d in (details.get("tournamentDivisions") or []):
                    if d.get("id") == div_id or d.get("tournamentDivisionId") == div_id:
                        div_obj  = d
                        div_name = _division_label(d)
                        break

        reg_open_at = _parse_iso((details or {}).get("registrationOpenAt")
                                 or (details or {}).get("registrationOpenDate"))
        new_status = (_reg_status_from_division(div_obj, reg_open_at, now_utc)
                      if div_obj else None)

        # Fallback: scrape the page text only when the API data can't decide.
        if new_status is None:
            try:
                await page.goto(url, wait_until="networkidle")
                page_text = await page.evaluate("() => document.body.innerText")
                new_status = _parse_reg_status(page_text)
                print(f"  [avail] (text fallback) {url}")
            except Exception as exc:
                print(f"  [avail] Error loading {url}: {exc}")
                continue

        is_new     = url not in url_ws
        ws         = url_ws.setdefault(url, {})
        old_status = ws.get("last_status", "unknown")
        ws.update({"last_status": new_status, "t_name": t_name, "div_name": div_name})

        label = f"{t_name}{' · ' + div_name if div_name else ''}"
        old_l = _REG_LABEL.get(old_status, old_status)
        new_l = _REG_LABEL.get(new_status, new_status)

        if is_new:
            print(f"  [avail] {label}: bootstrapped at '{new_l}' — will notify on improvement")
        else:
            print(f"  [avail] {label}: {old_l} → {new_l}")
            if _REG_RANK.get(new_status, 0) > _REG_RANK.get(old_status, 0):
                for user in users:
                    if url in {_url_entry_url(e) for e in (user.get("tournament_urls") or [])}:
                        send_email(
                            f"CBVA Registration Update — {label}",
                            _build_url_status_email(
                                url, old_status, new_status,
                                t_name, div_name, venue_str, t_date, now_pt,
                            ),
                            to=user["email"],
                        )
                        print(f"  [avail] Status email → {user['email']}")


def _build_new_tournament_email(records: list[dict], now_pt: datetime) -> str:
    """HTML email for newly discovered city-watch tournament divisions."""
    ts    = now_pt.strftime("%Y-%m-%d %H:%M PT")
    cards = ""
    for r in records:
        t_url    = r["t_url"]
        t_name   = r["t_name"]
        venue    = r["venue_str"]
        t_date   = r["t_date"]
        watch    = r["watch"]
        new_divs = r["new_divs"]

        meta = " · ".join(p for p in [venue, t_date] if p)

        filter_parts: list[str] = []
        if watch.get("genders"):
            filter_parts.append(", ".join(watch["genders"]))
        if watch.get("divisions"):
            filter_parts.append(", ".join(watch["divisions"]))
        filter_str = " · ".join(filter_parts)

        div_rows = ""
        for div in new_divs:
            div_id  = div.get("id") or div.get("tournamentDivisionId")
            d_label = _division_label(div)
            div_url = f"{t_url}/{div_id}"
            div_rows += (
                f"<tr><td style='padding:3px 0;font-size:13px'>"
                f"<a href='{div_url}' style='color:#1D9E75;text-decoration:none'>"
                f"🏐 {d_label}</a></td></tr>"
            )

        cards += f"""
<div style='border:1px solid #ddd;border-radius:8px;padding:16px;margin-bottom:14px'>
  <div style='font-size:16px;font-weight:700'>
    <a href='{t_url}' style='color:#1D9E75;text-decoration:none'>{t_name}</a>
  </div>
  <div style='font-size:13px;color:#555;margin:4px 0 2px'>{meta}</div>
  <div style='font-size:11px;color:#aaa;margin-bottom:10px'>
    Watch: {watch['city']}{' · ' + filter_str if filter_str else ''}
  </div>
  <table style='border-collapse:collapse'>{div_rows}</table>
  <a href='{t_url}' style='display:inline-block;margin-top:10px;font-size:12px;
     color:#1D9E75;text-decoration:none'>View tournament →</a>
</div>"""

    return (
        "<html><body style='font-family:sans-serif;max-width:640px;"
        "margin:0 auto;padding:24px;color:#222'>"
        "<h1 style='font-size:20px;border-bottom:2px solid #1D9E75;"
        f"padding-bottom:8px;color:#1D9E75'>🏐 New Tournaments Found</h1>"
        f"<p style='font-size:13px;color:#666'>{ts}</p>"
        f"{cards}"
        "</body></html>"
    )


def _register_deeplink(div_id: int) -> str:
    """
    Deep-link straight into CBVA's registration flow with the division
    pre-selected — the same target as the on-page "Register"/"Join Waitlist"
    button: /account/registrations?teams=[{"divisionId":<id>,"profileIds":[]}]
    Logged-in users land on the registration page with the division queued;
    logged-out users hit the login page first, then continue.
    """
    teams = json.dumps([{"divisionId": div_id, "profileIds": []}], separators=(",", ":"))
    return f"{BASE_URL}/account/registrations?teams={urllib.parse.quote(teams, safe='')}"


def _build_url_status_email(
    url: str,
    old_status: str,
    new_status: str,
    t_name: str,
    div_name: str,
    venue_str: str,
    t_date: str,
    now_pt: datetime,
) -> str:
    """HTML email for a tournament URL registration-status improvement."""
    ts    = now_pt.strftime("%Y-%m-%d %H:%M PT")
    old_l = _REG_LABEL.get(old_status, old_status)
    new_l = _REG_LABEL.get(new_status, new_status)
    title = t_name + (f" — {div_name}" if div_name else "")
    meta  = " · ".join(p for p in [venue_str, t_date] if p)
    emoji = "✅" if new_status == "open" else "🕐"
    color = "#1D9E75" if new_status == "open" else "#6A5ACD"
    btn   = "Register Now →" if new_status == "open" else "Join Waitlist →"

    # One-click register: deep-link into CBVA's registration flow with the
    # division pre-selected (open / waitlist only). Fall back to the tournament
    # page for any other status, or if we can't parse the division id.
    m       = re.search(r"/tournaments/\d+/(\d+)", url)
    div_id  = int(m.group(1)) if m else None
    if new_status in ("open", "waitlist") and div_id:
        cta_url, cta_text = _register_deeplink(div_id), btn
        cta_note = ("<div style='font-size:12px;color:#888;margin-top:8px'>"
                    "Opens CBVA registration with this division pre-selected — "
                    "sign in to confirm your team.</div>")
    else:
        cta_url, cta_text, cta_note = url, "View Tournament →", ""

    return (
        "<html><body style='font-family:sans-serif;max-width:640px;"
        "margin:0 auto;padding:24px;color:#222'>"
        f"<h1 style='font-size:20px;border-bottom:2px solid {color};"
        f"padding-bottom:8px;color:{color}'>{emoji} Registration Update</h1>"
        f"<p style='font-size:13px;color:#666'>{ts}</p>"
        "<div style='border:1px solid #ddd;border-radius:8px;padding:16px;margin:14px 0'>"
        f"<div style='font-size:16px;font-weight:700'>{title}</div>"
        f"<div style='font-size:13px;color:#555;margin:4px 0 12px'>{meta}</div>"
        "<div style='font-size:14px;margin-bottom:14px'>"
        f"<span style='color:#aaa;text-decoration:line-through'>{old_l}</span>"
        f"&nbsp;&rarr;&nbsp;"
        f"<span style='color:{color};font-weight:700'>{new_l}</span>"
        "</div>"
        f"<a href='{cta_url}' style='display:inline-block;padding:10px 22px;"
        f"background:{color};color:#fff;text-decoration:none;"
        f"border-radius:6px;font-size:14px;font-weight:600'>{cta_text}</a>"
        f"{cta_note}"
        f"<div style='margin-top:12px'><a href='{url}' "
        f"style='font-size:12px;color:#888'>View tournament details →</a></div>"
        "</div>"
        "</body></html>"
    )


# ── Player match records (pool + playoff, unified) ─────────────────────────────
#
# CBVA uses three id spaces: roster teamId (13xxxx, persistent), the per-
# tournament team "id" (22xxxx) that appears as teamAId/teamBId in BOTH pool and
# playoff matches, and (separately) registration seed vs playoffsSeed. The only
# reliable way to find a watched player's games is the 22xxxx id, which
# tournaments.getTeams maps from the roster teamId / profile id we already store.

def _build_teams_index(teams: list) -> dict:
    """From tournaments.getTeams → lookups for one division.

    Returns {by_roster, by_profile, seed, finish} where by_roster/by_profile map
    to the 22xxxx match-team id, and seed/finish are keyed by that id.
    """
    by_roster, by_profile, seed_of, finish_of = {}, {}, {}, {}
    for t in teams or []:
        mid = t.get("id")
        if mid is None:
            continue
        if t.get("teamId") is not None:
            by_roster[t["teamId"]] = mid
        for pid in (t.get("originalPlayerIds") or []):
            by_profile[pid] = mid
        for pl in ((t.get("team") or {}).get("players") or []):
            if pl.get("playerProfileId") is not None:
                by_profile[pl["playerProfileId"]] = mid
        seed_of[mid]   = t.get("seed")
        finish_of[mid] = t.get("finish")
    return {"by_roster": by_roster, "by_profile": by_profile,
            "seed": seed_of, "finish": finish_of}


def _resolve_match_team_id(entry: dict, idx: dict):
    """The 22xxxx match-team id for a watched player, via roster id then profile id."""
    tid = entry.get("team_id")
    if tid is not None and tid in idx["by_roster"]:
        return idx["by_roster"][tid]
    pid = entry.get("profile_id")
    if pid is not None and pid in idx["by_profile"]:
        return idx["by_profile"][pid]
    return None


def _perspective_score(sets: list, our_side: str) -> str:
    """Set scores from our player's perspective, skipping unplayed sets."""
    out = []
    for s in sets or []:
        a, b = s.get("teamAScore", 0), s.get("teamBScore", 0)
        if a == 0 and b == 0 and s.get("status") in ("not_started", None):
            continue
        out.append(f"{a}-{b}" if our_side == "A" else f"{b}-{a}")
    return ", ".join(out)


def _match_record(m: dict, mtid: int, phase: str, seed_of: dict, pool_name=None):
    """Annotate one raw match as a player-perspective record, or None if not ours."""
    a_id, b_id = m.get("teamAId"), m.get("teamBId")
    if mtid == a_id:
        our_side = "A"
    elif mtid == b_id:
        our_side = "B"
    else:
        return None
    opp_id  = b_id if our_side == "A" else a_id
    winner  = m.get("winnerId")
    return {
        "phase":     phase,                       # "pool" | "playoff"
        "pool_name": pool_name,
        "round":     m.get("round"),              # playoff only
        "match_no":  m.get("matchNumber"),
        "status":    m.get("status"),
        "score":     _perspective_score(m.get("sets"), our_side),
        "opp_seed":  seed_of.get(opp_id),
        "won":       (winner == mtid) if winner is not None else None,
        "court":     m.get("court"),
        "match_id":  m.get("id"),
    }


def _collect_player_records(mtid: int, pools: list, bracket: list, idx: dict) -> list:
    """All of a player's games (pool + playoff), matched by 22xxxx id."""
    seed_of = idx["seed"]
    recs = []
    for pool in (pools or []):
        for m in (pool.get("matches") or []):
            r = _match_record(m, mtid, "pool", seed_of, pool_name=pool.get("name"))
            if r:
                recs.append(r)
    for m in (bracket or []):
        r = _match_record(m, mtid, "playoff", seed_of)
        if r:
            recs.append(r)
    return recs


def _records_fingerprint(records: list) -> str:
    """Stable fingerprint of the player's COMPLETED games; changes → email."""
    done = sorted(
        [[r.get("match_id"), r.get("status"), r.get("score"), r.get("won")]
         for r in records if r.get("status") == "completed"],
        key=lambda x: (x[0] or 0),
    )
    return json.dumps(done, default=str)


_div_results_cache: dict = {}   # per-run: div_id → (teams_index, pools, bracket)


async def _fetch_division_results(page, tournament_id, div_id):
    """Fetch getTeams + getPools + getPlayoffs for a division (cached per run)."""
    if div_id in _div_results_cache:
        return _div_results_cache[div_id]
    await page.goto(f"{BASE_URL}/tournaments/{tournament_id}/{div_id}", wait_until="networkidle")
    teams   = await _trpc_get(page, "tournaments.getTeams",     {"tournamentDivisionId": div_id})
    pools   = await _trpc_get(page, "tournaments.getPools",     {"tournamentDivisionId": div_id})
    bracket = await _trpc_get(page, "tournaments.getPlayoffs",  {"tournamentDivisionId": div_id})
    res = (
        _build_teams_index(teams if isinstance(teams, list) else []),
        pools   if isinstance(pools, list)   else [],
        bracket if isinstance(bracket, list) else [],
    )
    _div_results_cache[div_id] = res
    return res


async def check_results(page, state: dict) -> tuple[list, list]:
    """
    For each confirmed-playing watchlist player, fetch latest results across BOTH
    pool play and the playoff bracket.
    Returns:
      score_updates   — (wname, entry, records) when a new game is completed
      playoff_updates — (wname, entry, records) when the player enters the bracket
    """
    td = state.get("tournament_day", {})
    playing = td.get("playing", {})
    if not playing:
        return [], []

    results_state   = td.setdefault("results", {})
    score_updates   = []
    playoff_updates = []
    _div_results_cache.clear()   # fresh data each run

    for wname, entry in playing.items():
        div_id = entry.get("division_id")
        if not div_id:
            continue

        idx, pools, bracket = await _fetch_division_results(
            page, entry.get("tournament_id", ""), div_id)

        mtid = _resolve_match_team_id(entry, idx)
        if mtid is None:
            print(f"  [results] {wname}: could not resolve match-team id — skipping")
            continue
        entry["match_team_id"] = mtid
        entry["finish"] = idx["finish"].get(mtid)
        td["playing"][wname] = entry

        records = _collect_player_records(mtid, pools, bracket, idx)
        r_state = results_state.setdefault(wname, {})

        # ── Playoff qualification: appears in a bracket match that has started ──
        if not r_state.get("playoff_notified", False):
            playoff_recs = [r for r in records if r["phase"] == "playoff"]
            # Gate on a started match so we don't fire on bracket initialisation
            # (CBVA publishes the full bracket "scheduled" before play begins).
            match_started = any(r["status"] not in ("scheduled", "not_started", None)
                                for r in playoff_recs)
            if playoff_recs and match_started:
                r_state["playoff_notified"] = True
                playoff_updates.append((wname, entry, records))
                print(f"  [playoffs] {wname}: entered bracket (team {mtid})")

        # ── Results fingerprint over completed games (pool + playoff) ──────────
        fp = _records_fingerprint(records)
        if fp != r_state.get("fingerprint", ""):
            r_state["fingerprint"]  = fp
            r_state["records"]      = records
            r_state["last_updated"] = datetime.now().isoformat()
            if fp != "[]":
                score_updates.append((wname, entry, records))
                n = len(json.loads(fp))
                print(f"  [results] {wname}: {n} completed game(s) — emailing")
            else:
                print(f"  [results] {wname}: no completed games yet")

    return score_updates, playoff_updates


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

def _t_date_short(obj: dict) -> str:
    """Short tournament date like 'Aug 8' for subject lines."""
    for field in ("date", "startDate", "scheduledDate", "tournamentDate"):
        raw = obj.get(field)
        if raw:
            try:
                return datetime.fromisoformat(
                    str(raw).replace("Z", "+00:00")
                ).strftime("%b %-d")
            except Exception:
                return ""
    return ""


def _date_str(now_pt: datetime) -> str:
    return f"{now_pt.strftime('%b')} {now_pt.day}, {now_pt.year}"


def _new_tournament_subject(records: list[dict]) -> str:
    """Subject naming the tournament(s) + date, not the run date (so 'Ocean
    Beach, Aug 8' reads clearly and repeats at the same venue are obvious)."""
    cities = ", ".join(dict.fromkeys(r["watch"]["city"] for r in records))
    labels = [
        r["t_name"] + (f", {r['t_date_short']}" if r.get("t_date_short") else "")
        for r in records
    ]
    if len(records) == 1:
        return f"New CBVA Tournament — {labels[0]} ({cities})"
    if len(records) <= 3:
        return f"New CBVA Tournaments in {cities} — " + "; ".join(labels)
    return f"{len(records)} new CBVA Tournaments in {cities}"

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


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"




def _record_sort_key(r: dict):
    # Pool games first (by match number), then playoff games (by round).
    if r.get("phase") == "pool":
        return (0, r.get("match_no") or 0)
    return (1, r.get("round") or 0)


def _format_results_body(records, entry: dict, roster: dict | None = None) -> str:
    """Render a player's completed games — both pool play and playoffs."""
    recs = [r for r in (records or []) if r.get("status") == "completed"]
    if not recs:
        return ""
    recs.sort(key=_record_sort_key)

    partner = entry.get("partner", "")
    t_url   = f"{BASE_URL}/tournaments/{entry.get('tournament_id','')}/{entry.get('division_id','')}"
    pool_w = pool_l = 0
    rows = []
    for r in recs:
        won = r.get("won")
        if r.get("phase") == "pool":
            if won is True:
                pool_w += 1
            elif won is False:
                pool_l += 1
            label = "Pool " + str(r.get("pool_name") or "").strip()
        else:
            label = f"Playoff · Round {(r.get('round') or 0) + 1}"

        opp_seed  = r.get("opp_seed")
        opp_names = (roster or {}).get(opp_seed, "") if opp_seed else ""
        if opp_names:
            opp_label = opp_names + (f" (#{opp_seed})" if opp_seed else "")
        elif opp_seed:
            opp_label = f"Seed #{opp_seed}"
        else:
            opp_label = "opponent"

        icon      = "✅ Win" if won else ("❌ Loss" if won is False else "•")
        color, bg = ("#1D9E75", "#f5faf8") if won else ("#cc3333", "#fff5f5")
        court     = r.get("court")
        rows.append(_CARD.format(
            color=color, bg=bg,
            title=f"{icon} — {label.strip()} vs {opp_label}",
            line2=f"Score: {r.get('score') or 'n/a'}" + (f"  ·  {court}" if court else ""),
            line3=f"Partner: {partner}",
        ))

    summary = ""
    if pool_w or pool_l:
        summary += (f"<p style='margin:.6em 0 .2em;font-size:13px;color:#555'>"
                    f"Pool record: <strong>{pool_w}-{pool_l}</strong></p>")
    finish = entry.get("finish")
    if finish:
        summary += (f"<p style='margin:.2em 0;font-size:13px;color:#555'>"
                    f"Finished: <strong>{_ordinal(finish)}</strong></p>")

    return "\n".join(rows) + summary + (
        f"<p style='margin:.5em 0;font-size:12px'><a href='{t_url}'>Full results →</a></p>")


def build_playoffs_email(updates: list[tuple], state: dict, now_pt: datetime) -> str:
    """'Made Playoffs!' email — fired once per player when their team enters the bracket."""
    div_rosters = state.get("tournament_day", {}).get("div_rosters", {})
    body = ""
    for wname, entry, records in updates:
        profile_url = state.get("players", {}).get(wname, {}).get("profile_url", "")
        t_url = f"{BASE_URL}/tournaments/{entry.get('tournament_id','')}/{entry.get('division_id','')}"
        roster = div_rosters.get(entry.get("division_id"), {})

        # First playoff game our team is slotted into
        playoff = sorted([r for r in records if r.get("phase") == "playoff"],
                         key=lambda r: (r.get("round") or 0))
        first    = playoff[0] if playoff else {}
        opp_seed = first.get("opp_seed")
        court    = first.get("court") or "TBD"

        opp_label = (roster.get(opp_seed, "") or
                     (f"Seed #{opp_seed}" if opp_seed else "TBD"))
        time_label = ""

        body += _player_header(wname, profile_url)
        body += _CARD.format(
            color="#6A0DAD", bg="#f9f5ff",
            title=f"🏐 Made Playoffs! — {entry.get('division', '')}",
            line2=f"{entry.get('tournament_name', '')} · {entry.get('venue', '')}",
            line3=f"First match vs {opp_label} · {court}{time_label} · Partner: {entry.get('partner','')}",
        )
        body += f"<p style='margin:.3em 0;font-size:12px'><a href='{t_url}'>View bracket →</a></p>"

    return _wrap(body, "CBVA Made Playoffs", now_pt)


def _records_summary(records: list, entry: dict) -> str:
    """Short 'pool 3-1 · out R2' style summary of a player's completed games."""
    recs    = [r for r in (records or []) if r.get("status") == "completed"]
    pool    = [r for r in recs if r.get("phase") == "pool"]
    playoff = sorted([r for r in recs if r.get("phase") == "playoff"],
                     key=lambda r: (r.get("round") or 0))
    bits = []
    if pool:
        w = sum(1 for r in pool if r.get("won") is True)
        l = sum(1 for r in pool if r.get("won") is False)
        bits.append(f"pool {w}-{l}")
    if playoff:
        last = playoff[-1]
        rnd  = (last.get("round") or 0) + 1
        if last.get("won") is True:
            bits.append(f"won R{rnd}")
        elif last.get("won") is False:
            bits.append(f"out R{rnd}")
    finish = entry.get("finish")
    if finish:
        bits.append(f"{_ordinal(finish)}")
    return " · ".join(bits)


def _results_subject(updates: list[tuple], state: dict) -> str:
    """Build a subject line that conveys outcome without opening the email."""
    parts = []
    for wname, entry, records in updates:
        first   = wname.split()[0]
        summary = _records_summary(records, entry)
        parts.append(f"{first} {summary}" if summary else first)
    return ("CBVA: " + " · ".join(parts)) if parts else "CBVA Results Update"


def build_results_email(updates: list[tuple], state: dict, now_pt: datetime) -> str:
    div_rosters = state.get("tournament_day", {}).get("div_rosters", {})
    body = ""
    for wname, entry, raw_data in updates:
        profile_url = state.get("players", {}).get(wname, {}).get("profile_url", "")
        t_url = f"{BASE_URL}/tournaments/{entry.get('tournament_id', '')}/{entry.get('division_id', '')}"
        roster = div_rosters.get(entry.get("division_id"), {})
        results_html = _format_results_body(raw_data, entry, roster)
        if not results_html:
            continue  # nothing to show (all matches still upcoming)
        body += _player_header(wname, profile_url)
        body += (
            f"<p style='margin:.3em 0;font-size:13px;color:#555'>"
            f"{entry.get('tournament_name','')} · {entry.get('venue','')} · {entry.get('division','')}"
            f" · <a href='{t_url}'>view results</a></p>"
        )
        body += results_html
    return _wrap(body, "CBVA Results Update", now_pt) if body else ""


# ── Diagnostics (Layer 2 + 3; structured for Layer 4 auto-repair) ────────────

def _git_sha() -> str:
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _record_error(phase: str, exc: Exception, endpoint: str | None = None) -> None:
    import traceback as tb
    raw = _trpc_responses.get(endpoint, "") if endpoint else ""
    if not raw:
        # Include last few captured responses for context
        raw = json.dumps({k: v[:400] for k, v in list(_trpc_responses.items())[-3:]}, indent=2)
    _run_errors.append({
        "phase":                phase,
        "exception_type":       type(exc).__name__,
        "message":              str(exc)[:500],
        "traceback":            tb.format_exc()[-3000:],
        "endpoint":             endpoint,
        "cbva_response_sample": raw[:3000] or None,
        "git_sha":              _git_sha(),
    })
    print(f"[error] {phase}: {type(exc).__name__}: {exc}")


def _send_diagnostic_email(errors: list[dict], now_pt: datetime) -> None:
    n  = len(errors)
    sha = errors[0].get("git_sha", "unknown") if errors else _git_sha()
    subject = f"🚨 CBVA Monitor error — {_date_str(now_pt)} ({n} issue{'s' if n > 1 else ''})"

    cards = ""
    for i, err in enumerate(errors, 1):
        response_block = ""
        if err.get("cbva_response_sample"):
            ep = err.get("endpoint") or "recent endpoints"
            response_block = (
                f"<h4 style='margin:.6em 0 .2em;color:#555'>Raw CBVA response ({ep})</h4>"
                f"<pre style='background:#fff8e1;padding:10px;font-size:11px;"
                f"border-radius:4px;overflow:auto;white-space:pre-wrap'>"
                f"{err['cbva_response_sample']}</pre>"
            )
        cards += (
            f"<div style='border:1px solid #f5c0c0;border-radius:8px;padding:16px;margin:12px 0;"
            f"background:#fff'>"
            f"<strong style='color:#cc3333'>Error {i}: {err['phase']} — {err['exception_type']}</strong>"
            f"<p style='margin:.4em 0;color:#444'>{err['message']}</p>"
            f"<pre style='background:#f5f5f5;padding:10px;font-size:11px;border-radius:4px;"
            f"overflow:auto;white-space:pre-wrap'>{err['traceback']}</pre>"
            f"{response_block}"
            f"</div>"
        )

    body = (
        f"<p><strong>Time:</strong> {now_pt.strftime('%Y-%m-%d %H:%M PT')} &nbsp;"
        f"<strong>Git SHA:</strong> <code>{sha}</code></p>"
        f"{cards}"
        f"<p style='margin-top:1.5em;font-size:12px;color:#888'>"
        f"<a href='https://github.com/suacide24/cbva-monitor/actions'>View Actions →</a>"
        f" · Diagnostic structured for Layer 4 auto-repair.</p>"
    )
    send_email(subject, _wrap(body, "CBVA Monitor Diagnostic", now_pt), to=EMAIL_FROM)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    global _run_errors, _trpc_responses
    _run_errors     = []
    _trpc_responses = {}

    users = load_users()
    if not users:
        print("No users in users.json — nothing to do.")
        return

    now_pt     = datetime.now(_PT)
    today_pt   = now_pt.date()
    today_str  = today_pt.strftime("%Y-%m-%d")
    is_weekend = today_pt.weekday() >= 5   # Saturday=5, Sunday=6
    all_players: list[str] = []            # populated in the weekend branch

    state = load_state()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()
        await login(page)

        # ── Phase 0: Tournament Availability Tracker (runs every day) ─────
        try:
            await check_city_tournaments(page, state, users, now_pt)
            await check_url_statuses(page, state, users, now_pt)
        except Exception as exc:
            _record_error("tournament_availability", exc)

        if not is_weekend:
            print(f"\nToday is {today_pt.strftime('%A')} PT — player tracking is weekends only.")
        else:
            all_players = sorted({pl for u in users for pl in u["players"]})
            print(f"\nCBVA monitor — {today_pt.strftime('%A, %B')} {today_pt.day}, {today_pt.year} PT")
            print(f"{len(users)} user(s) · {len(all_players)} unique player(s): {', '.join(all_players)}")

            # ── Phase 1: Ratings (once per day) ───────────────────────────
            try:
                if state.get("last_rating_check") != today_str:
                    print("\n── Rating checks ─────────────────────────────────────────")
                    rating_alerts = await check_ratings(page, state, all_players)
                    state["last_rating_check"] = today_str
                    for user in users:
                        user_alerts = [a for a in rating_alerts if a["name"] in user["players"]]
                        if user_alerts:
                            names_str = ", ".join(a["name"] for a in user_alerts)
                            send_email(
                                f"CBVA Rating Alert — {_date_str(now_pt)}: {names_str}",
                                build_rating_email(user_alerts, now_pt),
                                to=user["email"],
                            )
                else:
                    print("\n[ratings] Already checked today — skipping.")
            except Exception as exc:
                _record_error("ratings", exc, endpoint="profiles.getOverview")

            # ── Phase 2: Tournament roster scan ───────────────────────────
            new_entries: list[dict] = []
            try:
                print("\n── Tournament scan ───────────────────────────────────────────")
                new_entries = await scan_today_tournaments(page, today_str, state, all_players)
            except Exception as exc:
                _record_error("scan", exc, endpoint="tournaments.getTeams")

            td       = state.get("tournament_day", {})
            notified = td.get("notified", {})
            if isinstance(notified, list):
                notified = {u["email"]: list(notified) for u in users}
                td["notified"] = notified

            # ── Phase 3: "Playing Today" emails ───────────────────────────
            try:
                for user in users:
                    already  = set(notified.get(user["email"], []))
                    user_new = [e for e in new_entries
                                if e["player_name"] in user["players"]
                                and e["player_name"] not in already]
                    if user_new:
                        names_playing = [e["player_name"] for e in user_new]
                        print(f"\nPlaying today (new for {user['email']}): {', '.join(names_playing)}")
                        send_email(
                            f"CBVA Playing Today — {_date_str(now_pt)}: {', '.join(names_playing)}",
                            build_playing_today_email(user_new, state, now_pt),
                            to=user["email"],
                        )
                        notified.setdefault(user["email"], []).extend(names_playing)

                if not new_entries:
                    n_total = len(td.get("playing", {}))
                    print(f"\n[scan] No new players found. {n_total} confirmed on rosters.")
            except Exception as exc:
                _record_error("playing_today_email", exc)

            # ── Phase 4: Update players.json ──────────────────────────────
            try:
                div_rosters = td.get("div_rosters", {})
                if div_rosters:
                    new_names: set[str] = set()
                    for roster in div_rosters.values():
                        for team_str in roster.values():
                            for name in team_str.split(" / "):
                                if name.strip():
                                    new_names.add(name.strip())
                    try:
                        with open(PLAYERS_FILE) as f:
                            existing: set[str] = set(json.load(f))
                    except (FileNotFoundError, json.JSONDecodeError):
                        existing = set()
                    merged = sorted(existing | new_names)
                    with open(PLAYERS_FILE, "w") as f:
                        json.dump(merged, f, indent=2)
                    added = len(new_names - existing)
                    print(f"\n[players] {len(merged)} known players" + (f" (+{added} new)" if added else ""))
            except Exception as exc:
                _record_error("players_file", exc)

            # ── Phase 5: Results + playoff qualification ───────────────────
            try:
                playing = td.get("playing", {})
                if playing:
                    print(f"\n── Results check ({len(playing)} player(s)) ──────────────────")
                    score_updates, playoff_updates = await check_results(page, state)

                    for user in users:
                        user_set = set(user["players"])

                        user_playoffs = [(w, e, d) for w, e, d in playoff_updates if w in user_set]
                        if user_playoffs:
                            names_str = ", ".join(w for w, _, __ in user_playoffs)
                            send_email(
                                f"CBVA Made Playoffs! — {_date_str(now_pt)}: {names_str}",
                                build_playoffs_email(user_playoffs, state, now_pt),
                                to=user["email"],
                            )
                            print(f"Playoffs email → {user['email']} ({len(user_playoffs)} player(s)).")

                        user_scores = [(w, e, d) for w, e, d in score_updates if w in user_set]
                        if user_scores:
                            results_html = build_results_email(user_scores, state, now_pt)
                            if results_html:
                                send_email(
                                    _results_subject(user_scores, state),
                                    results_html,
                                    to=user["email"],
                                )
                                print(f"Results email → {user['email']} ({len(user_scores)} player(s)).")

                    if not score_updates and not playoff_updates:
                        print("[results] No changes since last run.")
                else:
                    print("\n[results] No confirmed players yet — skipping results check.")
            except Exception as exc:
                _record_error("results", exc, endpoint="tournaments.getPlayoffs")

        await browser.close()

    # ── Write run summary ──────────────────────────────────────────────────
    # The committed state.json keeps only the *meaningful* part of last_run
    # (status + structured errors, consumed by check_health.py / auto_repair.py).
    # The volatile heartbeat (timestamp, git_sha, players_watching) goes to an
    # uncommitted status.json so state.json no longer churns on every run —
    # liveness is tracked via the GitHub Actions API instead (see check_health.py).
    state["last_run"] = {
        "status":      "error" if _run_errors else "ok",
        "error_count": len(_run_errors),
        "errors":      _run_errors,
    }
    save_state(state)

    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({
                "timestamp":        now_pt.isoformat(),
                "git_sha":          _git_sha(),
                "status":           "error" if _run_errors else "ok",
                "players_watching": len(all_players),
                "error_count":      len(_run_errors),
            }, f, indent=2)
    except OSError as exc:
        print(f"[status] Could not write {STATUS_FILE}: {exc}")

    print("\nState saved.")

    if _run_errors:
        print(f"\n[diagnostic] {len(_run_errors)} error(s) — sending diagnostic email.")
        _send_diagnostic_email(_run_errors, now_pt)


if __name__ == "__main__":
    asyncio.run(run())
