#!/usr/bin/env python3
"""
Probe getTeams full response + match/result endpoints.
"""
import asyncio, json, os, urllib.parse
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")

# Division with rosterPublished=True from previous probe
KNOWN_DIV_ID  = 17119   # Girls 12U at Central Beach, June 7 2026
KNOWN_T_ID    = 4825

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

        # Navigate to the division page first to set context
        await page.goto(f"{BASE_URL}/tournaments/{KNOWN_T_ID}/{KNOWN_DIV_ID}", wait_until="networkidle")
        await page.wait_for_timeout(3_000)

        # Full getTeams response
        print(f"\n=== tournaments.getTeams(tournamentDivisionId={KNOWN_DIV_ID}) ===")
        raw = await fetch_trpc(page, "tournaments.getTeams", {"tournamentDivisionId": KNOWN_DIV_ID})
        print(raw)  # full response

        # Try match/results endpoints
        print(f"\n=== Match / result endpoint attempts ===")
        for ep, inp in [
            ("tournaments.getMatches",     {"tournamentDivisionId": KNOWN_DIV_ID}),
            ("tournaments.getMatches",     {"divisionId": KNOWN_DIV_ID}),
            ("tournaments.getResults",     {"tournamentDivisionId": KNOWN_DIV_ID}),
            ("tournaments.getResults",     {"id": KNOWN_T_ID, "divisionId": KNOWN_DIV_ID}),
            ("tournaments.getBracket",     {"tournamentDivisionId": KNOWN_DIV_ID}),
            ("tournaments.getPools",       {"tournamentDivisionId": KNOWN_DIV_ID}),
            ("tournaments.getPools",       {"id": KNOWN_DIV_ID}),
            ("pools.get",                  {"tournamentDivisionId": KNOWN_DIV_ID}),
            ("pools.list",                 {"tournamentDivisionId": KNOWN_DIV_ID}),
            ("pools.getAll",               {"tournamentDivisionId": KNOWN_DIV_ID}),
        ]:
            try:
                raw = await fetch_trpc(page, ep, inp)
                parsed = json.loads(raw)
                if "error" not in raw[:30]:
                    print(f"\n  *** HIT: {ep}({inp}):\n{raw[:1000]}")
                else:
                    msg = parsed.get("error",{}).get("json",{}).get("message","?")[:80]
                    print(f"  {ep}: {msg}")
            except Exception as ex:
                print(f"  {ep}: {ex}")

        # Also intercept ALL calls made when visiting the page
        print(f"\n=== All tRPC calls on /tournaments/{KNOWN_T_ID}/{KNOWN_DIV_ID} ===")
        all_calls = []
        async def cap(resp):
            if "cbva.com/api/trpc" in resp.url:
                all_calls.append(resp.url)
        page.on("response", cap)
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(4_000)
        for u in all_calls:
            print(f"  {u}")
            # Read each one via fetch
            path_parts = u.split("/api/trpc/")
            if len(path_parts) > 1:
                endpoint_part = path_parts[1].split("?")[0]
                input_part = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("input", ["{}"])[0]
                try:
                    inp_obj = json.loads(input_part)
                    raw2 = await fetch_trpc(page, endpoint_part, inp_obj.get("json", {}))
                    if "error" not in raw2[:30]:
                        print(f"    BODY: {raw2[:600]}")
                except Exception as ex:
                    pass

        await browser.close()

asyncio.run(probe())
