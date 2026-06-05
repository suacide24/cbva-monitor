#!/usr/bin/env python3
"""
Probe a PAST tournament (roster published) to understand bracket/player structure.
"""
import asyncio, json, os, urllib.parse
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")

async def fetch_trpc(page, endpoint, input_obj):
    raw_input = json.dumps({"json": input_obj})
    enc = urllib.parse.quote(raw_input)
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
    return raw

async def probe():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        await page.goto(BASE_URL, wait_until="networkidle")
        link = await page.query_selector("a[href*='login'], a:has-text('Log')")
        if link:
            await link.click()
            await page.wait_for_timeout(2_000)
        await page.fill("input[type='email'], input[name='email']", CBVA_EMAIL)
        await page.fill("input[type='password']", CBVA_PASSWORD)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3_000)
        print("[auth] Logged in")

        # Find a past tournament from Ramon's results
        # We know "Surf City Days" was on 9/6/25 at Huntington Pier
        print("\n=== Search for past tournament on 2025-09-06 ===")
        raw = await fetch_trpc(page, "tournaments.search", {"date": "2025-09-06"})
        result = json.loads(raw).get("result", {}).get("data", {}).get("json", {})
        data = result.get("data", [])
        print(f"Found {len(data)} tournaments")
        past_t = None
        past_div = None
        for t in data:
            print(f"  id={t['id']} name={t.get('name')} venue={t.get('venue',{}).get('name')} date={t['date']}")
            divs = t.get("tournamentDivisions", [])
            for d in divs:
                print(f"    div id={d['id']} gender={d['gender']} div={d.get('division',{}).get('name')} rosterPublished={d.get('rosterPublished')}")
                if d.get("rosterPublished") and past_t is None:
                    past_t = t
                    past_div = d

        if past_t and past_div:
            tid = past_t["id"]
            did = past_div["id"]
            print(f"\n=== Using tournament {tid} division {did} (rosterPublished=True) ===")

            # Visit the division page and read text
            await page.goto(f"{BASE_URL}/tournaments/{tid}/{did}", wait_until="networkidle")
            await page.wait_for_timeout(5_000)
            text = await page.evaluate("() => document.body.innerText")
            print("PAGE TEXT:")
            for i, line in enumerate(text.splitlines()):
                if line.strip():
                    print(f"  {i:03}: {repr(line.strip())}")

            # Intercept the page's API calls
            all_calls = []
            async def cap(resp):
                if "cbva.com/api/trpc" in resp.url and "getDirectors" not in resp.url and "settings" not in resp.url:
                    all_calls.append(resp.url)
            page.on("response", cap)
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(5_000)
            print(f"\nIntercepted URLs on reload:")
            for u in all_calls:
                print(f"  {u[:120]}")

            # Try roster-specific endpoints
            print("\n=== Roster endpoint attempts ===")
            for ep, inp in [
                ("tournaments.getRoster", {"tournamentDivisionId": did}),
                ("tournaments.getRoster", {"id": did}),
                ("tournaments.getTeams",  {"tournamentDivisionId": did}),
                ("tournaments.getTeams",  {"id": tid, "divisionId": did}),
                ("registrations.list",    {"tournamentDivisionId": did}),
                ("registrations.list",    {"tournamentId": tid, "divisionId": did}),
                ("registrations.getTeams", {"tournamentDivisionId": did}),
                ("matches.list",           {"tournamentDivisionId": did}),
                ("matches.getByDivision",  {"tournamentDivisionId": did}),
                ("pools.list",             {"tournamentDivisionId": did}),
                ("pools.get",              {"tournamentDivisionId": did}),
                ("brackets.list",          {"tournamentDivisionId": did}),
            ]:
                try:
                    raw = await fetch_trpc(page, ep, inp)
                    parsed = json.loads(raw)
                    if "error" not in raw[:30]:
                        print(f"  *** HIT: {ep}({inp}):\n    {raw[:800]}")
                    else:
                        msg = parsed.get("error",{}).get("json",{}).get("message","?")[:60]
                        print(f"  {ep}: {msg}")
                except Exception as ex:
                    print(f"  {ep}: {ex}")

            # Full tournaments.get for past tournament
            print(f"\n=== Full tournaments.get id={tid} ===")
            raw = await fetch_trpc(page, "tournaments.get", {"id": tid})
            print(raw[:4000])
        else:
            print("No past tournament with rosterPublished=True found")
            # Try an earlier date
            for d in ["2025-06-07", "2025-08-02", "2026-05-30"]:
                raw = await fetch_trpc(page, "tournaments.search", {"date": d})
                result = json.loads(raw).get("result",{}).get("data",{}).get("json",{})
                data = result.get("data", [])
                print(f"\nDate {d}: {len(data)} tournaments")
                for t in data[:2]:
                    divs = t.get("tournamentDivisions", [])
                    published = [d for d in divs if d.get("rosterPublished")]
                    print(f"  id={t['id']} venue={t.get('venue',{}).get('name')} published_divs={len(published)}")

        await browser.close()

asyncio.run(probe())
