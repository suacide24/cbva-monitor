#!/usr/bin/env python3
"""
Probe CBVA tournament bracket - read page text + try more endpoints.
"""
import asyncio, json, os, urllib.parse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")

async def fetch_trpc(page, endpoint, input_obj):
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
    return raw

async def probe():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        # Login
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

        # Intercept ALL tRPC responses including streaming
        all_calls = []
        async def cap(resp):
            if "cbva.com/api/trpc" in resp.url:
                try:
                    # Read full streaming body via fetch from browser instead
                    all_calls.append(resp.url)
                except: pass
        page.on("response", cap)

        # Visit the Dockweiler Men's A division (June 6) — first division of 4633
        print("\n=== Division page /tournaments/4633/16397 ===")
        await page.goto(f"{BASE_URL}/tournaments/4633/16397", wait_until="networkidle")
        await page.wait_for_timeout(5_000)

        # Print full page text
        text = await page.evaluate("() => document.body.innerText")
        print("PAGE TEXT:")
        for i, line in enumerate(text.splitlines()):
            if line.strip():
                print(f"  {i:03}: {repr(line.strip())}")

        print(f"\nIntercepted URLs: {all_calls}")

        # Try to fetch the division roster using page.evaluate fetch with full stream
        print("\n=== Trying roster endpoints via browser fetch ===")
        more_endpoints = [
            ("tournaments.getRoster",        {"tournamentDivisionId": 16397}),
            ("tournaments.getRoster",        {"divisionId": 16397}),
            ("tournaments.getResults",       {"id": 4633}),
            ("tournaments.getResults",       {"tournamentDivisionId": 16397}),
            ("registrations.list",           {"tournamentDivisionId": 16397}),
            ("registrations.getByDivision",  {"tournamentDivisionId": 16397}),
            ("profiles.getByTournament",     {"tournamentDivisionId": 16397}),
            ("tournaments.get",              {"id": 4633, "includeDivisions": True}),
            ("tournaments.get",              {"id": 4633, "includeRegistrations": True}),
        ]
        for ep, inp in more_endpoints:
            try:
                raw = await fetch_trpc(page, ep, inp)
                if "NOT_FOUND" not in raw and "error" not in raw[:50]:
                    print(f"  *** HIT: {ep}({inp}): {raw[:600]}")
                else:
                    err = json.loads(raw).get("error",{}).get("json",{}).get("message","?")
                    print(f"  {ep}: {err[:60]}")
            except Exception as ex:
                print(f"  {ep}: {ex}")

        # Also read full tournaments.get for tomorrow's tournament
        print("\n=== tournaments.get id=4633 (full) ===")
        raw = await fetch_trpc(page, "tournaments.get", {"id": 4633})
        print(raw[:3000])

        await browser.close()

asyncio.run(probe())
