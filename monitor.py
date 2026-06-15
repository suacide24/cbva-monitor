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
    import time
    for attempt in range(3):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(EMAIL_FROM, EMAIL_PASS)
                s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
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
                        for candidate in PLAYER_NAMES:
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
                    td["playing"][wname] = entry
                    print(f"    Found: {wname} · {div_label} · partner: {partner}")

                    if wname not in notified:
                        new_entries.append(entry)

    return new_entries


# ── Results tracking ──────────────────────────────────────────────────────────

# Confirmed working endpoints first, then fallbacks.
_RESULTS_CANDIDATES = [
    ("tournaments.getPlayoffs",        lambda d: {"tournamentDivisionId": d}),  # ✅ confirmed
    ("tournaments.getDivisionSummary", lambda d: {"tournamentDivisionId": d}),  # intercepted on page load
    ("tournaments.getSchedule",        lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getGames",           lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getDivisionGames",   lambda d: {"tournamentDivisionId": d}),
    ("tournaments.getBracket",         lambda d: {"tournamentDivisionId": d}),
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


def _player_fingerprint(data, entry: dict) -> str:
    """Fingerprint only the in_progress/completed matches for our player.

    Upcoming matches are covered by the Made Playoffs email; we only want to
    trigger a results email when something actually happens (score changes,
    match completes).  Filtering to active matches also prevents spurious
    emails when the bracket is first published with all matches as "upcoming".
    """
    our_seed    = entry.get("team_seed")
    our_tid     = entry.get("team_id")
    bracket_tid = entry.get("bracket_team_id")

    relevant = []
    for m in (data if isinstance(data, list) else []):
        if m.get("status") != "completed":
            continue
        a_seed, b_seed = m.get("teamASeed"), m.get("teamBSeed")
        a_id,   b_id   = m.get("teamAId"),   m.get("teamBId")
        if our_seed and (a_seed == our_seed or b_seed == our_seed):
            relevant.append(m)
        elif our_tid and (a_id == our_tid or b_id == our_tid):
            relevant.append(m)
        elif bracket_tid and (a_id == bracket_tid or b_id == bracket_tid):
            relevant.append(m)

    return json.dumps(relevant, sort_keys=True, default=str)


async def check_results(page, state: dict) -> tuple[list, list]:
    """
    For each confirmed-playing watchlist player, fetch latest results.
    Returns:
      score_updates   — (wname, entry, data) where scores/status changed
      playoff_updates — (wname, entry, data) where player newly appeared in bracket
    """
    td = state.get("tournament_day", {})
    playing = td.get("playing", {})
    if not playing:
        return [], []

    results_state  = td.setdefault("results", {})
    score_updates  = []
    playoff_updates = []

    for wname, entry in playing.items():
        div_id = entry.get("division_id")
        if not div_id:
            continue

        await page.goto(
            f"{BASE_URL}/tournaments/{entry.get('tournament_id', '')}/{div_id}",
            wait_until="networkidle",
        )
        data = await fetch_results(page, div_id)
        if data is None:
            continue

        r_state = results_state.setdefault(wname, {})

        # ── Discover bracket team_id from seeded matches ───────────────────
        # CBVA uses different team IDs in the roster vs the playoff bracket.
        # We capture the bracket ID the first time we find our player by seed
        # so that in later rounds (where seeds become null) we can still track them.
        our_seed    = entry.get("team_seed")
        team_id     = entry.get("team_id")
        bracket_tid = entry.get("bracket_team_id")
        if not bracket_tid and our_seed and isinstance(data, list):
            for m in data:
                a_seed, b_seed = m.get("teamASeed"), m.get("teamBSeed")
                a_id,   b_id   = m.get("teamAId"),   m.get("teamBId")
                if a_seed == our_seed and a_id:
                    bracket_tid = a_id
                elif b_seed == our_seed and b_id:
                    bracket_tid = b_id
                if bracket_tid:
                    entry["bracket_team_id"] = bracket_tid
                    td["playing"][wname] = entry
                    print(f"  [bracket] {wname}: bracket_team_id={bracket_tid}")
                    break

        # ── Playoff qualification check ────────────────────────────────────
        if not r_state.get("playoff_notified", False) and isinstance(data, list) and data:
            in_bracket = False
            if team_id:
                in_bracket = any(
                    m.get("teamAId") == team_id or m.get("teamBId") == team_id
                    for m in data
                )
            if not in_bracket and bracket_tid:
                in_bracket = any(
                    m.get("teamAId") == bracket_tid or m.get("teamBId") == bracket_tid
                    for m in data
                )
            if not in_bracket and our_seed:
                in_bracket = any(
                    m.get("teamASeed") == our_seed or m.get("teamBSeed") == our_seed
                    for m in data
                )
            if in_bracket:
                r_state["playoff_notified"] = True
                playoff_updates.append((wname, entry, data))
                match_method = ("team_id" if team_id else
                                f"bracket_team_id #{bracket_tid}" if bracket_tid else
                                f"seed #{our_seed}")
                print(f"  [playoffs] {wname}: appeared in bracket via {match_method}!")

        # ── Score / status fingerprint (scoped to our player's active matches) ──
        fp = _player_fingerprint(data, entry)
        old_fp = r_state.get("fingerprint", "")
        if fp != old_fp:
            r_state["fingerprint"]  = fp
            r_state["raw"]          = data
            r_state["last_updated"] = datetime.now().isoformat()
            if fp != "[]":
                # Real change to an in_progress/completed match — email worthy
                score_updates.append((wname, entry, data))
                print(f"  [results] {wname}: results changed")
            else:
                print(f"  [results] {wname}: fingerprint reset (no active matches yet)")

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


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _finish_place(all_matches: list, lost_round: int) -> str:
    """Return a place string like '9th–16th' for a player eliminated in lost_round.

    Works by walking rounds from the final downward.  Losers of the final are
    2nd; losers of the semi are 3rd–4th; losers of the quarter are 5th–8th, etc.
    Uses actual per-round match counts so non-power-of-2 brackets are handled.
    """
    rounds_desc = sorted({m.get("round", 0) for m in all_matches}, reverse=True)
    lo = 2  # positions start after 1st place (the champion)
    for r in rounds_desc:
        count = sum(1 for m in all_matches if m.get("round") == r)
        if r == lost_round:
            hi = lo + count - 1
            return _ordinal(lo) if lo == hi else f"{_ordinal(lo)}–{_ordinal(hi)}"
        lo += count
    return ""


def _score_str(sets: list) -> str:
    """Turn a sets array into a readable score like '21-15, 21-18'."""
    parts = []
    for s in sets or []:
        a, b = s.get("teamAScore", 0), s.get("teamBScore", 0)
        if a == 0 and b == 0 and s.get("status") in ("not_started", None):
            continue
        parts.append(f"{a}-{b}")
    return ", ".join(parts) if parts else "not started"


def _build_seed_map(matches: list) -> dict[int, int]:
    """Build team_id → original seed from rounds where seed data is populated.

    CBVA nulls out seeds in round 2+ bracket matches, but the seed is always
    present in the round where the team first entered.  We scan all matches
    once to build a lookup so later rounds can still show a meaningful seed.
    """
    seed_map: dict[int, int] = {}
    for m in matches:
        if m.get("teamAId") and m.get("teamASeed"):
            seed_map[m["teamAId"]] = m["teamASeed"]
        if m.get("teamBId") and m.get("teamBSeed"):
            seed_map[m["teamBId"]] = m["teamBSeed"]
    return seed_map


def _format_results_body(data, entry: dict, roster: dict | None = None) -> str:
    """Render bracket (getPlayoffs) results for the tracked player."""
    if not data:
        return "<p style='color:#888'>No result data.</p>"

    our_seed    = entry.get("team_seed")
    our_tid     = entry.get("team_id")
    bracket_tid = entry.get("bracket_team_id")
    partner     = entry.get("partner", "")
    t_url      = f"{BASE_URL}/tournaments/{entry.get('tournament_id','')}/{entry.get('division_id','')}"
    matches    = data if isinstance(data, list) else []

    # Sort chronologically so history reads top-to-bottom
    matches = sorted(matches, key=lambda m: (m.get("round") or 0))

    # Build seed lookup so round 2+ opponents can be resolved by original seed
    seed_map = _build_seed_map(matches)

    max_round    = max((m.get("round", 0) for m in matches), default=0)
    finish_round = None   # round where player was eliminated (lost)
    is_champion  = False

    rows = []
    for m in matches:
        a_seed, b_seed = m.get("teamASeed"), m.get("teamBSeed")
        a_id,   b_id   = m.get("teamAId"),   m.get("teamBId")
        status         = m.get("status", "")
        winner_id      = m.get("winnerId")
        round_num      = m.get("round", 0)
        sets           = m.get("sets", [])
        court          = m.get("court", "")

        # Only render completed matches — upcoming and in-progress are not shown
        if status != "completed":
            continue

        # Is our player in this match?
        our_side = None
        if our_seed and (a_seed == our_seed or b_seed == our_seed):
            our_side = "A" if a_seed == our_seed else "B"
        elif our_tid and (a_id == our_tid or b_id == our_tid):
            our_side = "A" if a_id == our_tid else "B"
        elif bracket_tid and (a_id == bracket_tid or b_id == bracket_tid):
            our_side = "A" if a_id == bracket_tid else "B"

        if our_side is None and our_seed is None and our_tid is None and bracket_tid is None:
            our_side = "show"

        if our_side is None:
            continue

        opp_id   = b_id if our_side == "A" else a_id
        opp_seed = b_seed if our_side == "A" else a_seed
        if not opp_seed and opp_id:
            opp_seed = seed_map.get(opp_id)
        opp_names = (roster or {}).get(opp_seed, "") if opp_seed else ""
        score    = _score_str(sets)
        if our_side == "B":
            flipped = []
            for s in sets or []:
                a, b = s.get("teamAScore", 0), s.get("teamBScore", 0)
                if a == 0 and b == 0 and s.get("status") in ("not_started", None):
                    continue
                flipped.append(f"{b}-{a}")
            score = ", ".join(flipped) if flipped else "not started"

        won = (winner_id == a_id and our_side == "A") or (winner_id == b_id and our_side == "B")
        if won and round_num == max_round:
            is_champion = True
        elif not won:
            finish_round = round_num

        result_icon = "✅ Win" if won else "❌ Loss"
        color, bg   = ("#1D9E75", "#f5faf8") if won else ("#cc3333", "#fff5f5")

        rnd_label = f"Round {round_num + 1}" if round_num is not None else "Match"
        if opp_names:
            opp_label = opp_names + (f" (#{opp_seed})" if opp_seed else "")
        elif opp_seed:
            opp_label = f"Seed #{opp_seed}"
        else:
            opp_label = "TBD"

        rows.append(_CARD.format(
            color=color, bg=bg,
            title=f"{result_icon} — {rnd_label} vs {opp_label}",
            line2=f"Score: {score}" + (f"  ·  {court}" if court else ""),
            line3=f"Partner: {partner}",
        ))

    if not rows:
        return ""

    # Append finishing place when the tournament result is known
    place_html = ""
    if is_champion:
        place_html = "<p style='margin:.6em 0;font-weight:bold;color:#1D9E75'>🏆 1st place — Tournament Champions</p>"
    elif finish_round is not None:
        place = _finish_place(matches, finish_round)
        if place:
            place_html = f"<p style='margin:.6em 0;font-size:13px;color:#555'>Finished: <strong>{place}</strong></p>"

    return "\n".join(rows) + place_html + f"<p style='margin:.5em 0;font-size:12px'><a href='{t_url}'>Full bracket →</a></p>"


def build_playoffs_email(updates: list[tuple], state: dict, now_pt: datetime) -> str:
    """'Made Playoffs!' email — fired once per player when their team enters the bracket."""
    body = ""
    for wname, entry, playoffs in updates:
        profile_url = state.get("players", {}).get(wname, {}).get("profile_url", "")
        t_url = f"{BASE_URL}/tournaments/{entry.get('tournament_id','')}/{entry.get('division_id','')}"
        team_id = entry.get("team_id")

        # Find the first match our team is slotted into
        our_match = next(
            (m for m in (playoffs or [])
             if m.get("teamAId") == team_id or m.get("teamBId") == team_id),
            None,
        )
        our_side   = "A" if (our_match or {}).get("teamAId") == team_id else "B"
        opp_tid    = (our_match or {}).get("teamBId" if our_side == "A" else "teamAId")
        court      = (our_match or {}).get("court", "TBD")
        sched_time = (our_match or {}).get("scheduledTime", "")

        opp_label  = f"Team #{opp_tid}" if opp_tid else "TBD"
        time_label = f" · {sched_time}" if sched_time else ""

        body += _player_header(wname, profile_url)
        body += _CARD.format(
            color="#6A0DAD", bg="#f9f5ff",
            title=f"🏐 Made Playoffs! — {entry.get('division', '')}",
            line2=f"{entry.get('tournament_name', '')} · {entry.get('venue', '')}",
            line3=f"First match vs {opp_label} · {court}{time_label} · Partner: {entry.get('partner','')}",
        )
        body += f"<p style='margin:.3em 0;font-size:12px'><a href='{t_url}'>View bracket →</a></p>"

    return _wrap(body, "CBVA Made Playoffs", now_pt)


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
            f" · <a href='{t_url}'>view bracket</a></p>"
        )
        body += results_html
    return _wrap(body, "CBVA Results Update", now_pt) if body else ""


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

        # ── Results + playoff qualification ───────────────────────────────
        playing = state.get("tournament_day", {}).get("playing", {})
        if playing:
            print(f"\n── Results check ({len(playing)} player(s)) ──────────────────")
            score_updates, playoff_updates = await check_results(page, state)

            # Playoff qualification email (fires once per player, before scores)
            if playoff_updates:
                names_str = ", ".join(u[0] for u in playoff_updates)
                send_email(
                    f"CBVA Made Playoffs! — {_date_str(now_pt)}: {names_str}",
                    build_playoffs_email(playoff_updates, state, now_pt),
                )
                print(f"Playoffs email sent for {len(playoff_updates)} player(s).")

            # Score/status update email
            if score_updates:
                results_html = build_results_email(score_updates, state, now_pt)
                if results_html:
                    send_email(
                        f"CBVA Results Update — {now_pt.strftime('%b')} {now_pt.day} "
                        f"{now_pt.strftime('%I:%M %p')} PT",
                        results_html,
                    )
                    print(f"Results update sent for {len(score_updates)} player(s).")
                else:
                    print("[results] Score changed but no in-progress/completed matches to show — skipping email.")

            if not score_updates and not playoff_updates:
                print("[results] No changes since last run.")
        else:
            print("\n[results] No confirmed players yet — skipping results check.")

        await browser.close()

    save_state(state)
    print("\nState saved.")


if __name__ == "__main__":
    asyncio.run(run())
