#!/usr/bin/env python3
"""
Probe CBVA tournament bracket / division API.
"""
import asyncio, json, os, urllib.parse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")
TODAY         = datetime.now().strftime("%Y-%m-%d")
TOMORROW      = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

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
        login_link = await page.query_selector("a[href*='login'], a:has-text('Log')")
        if login_link:
            await login_link.click()
            await page.wait_for_timeout(2_000)
        try:
            await page.fill("input[type='email'], input[name='email']", CBVA_EMAIL)
            await page.fill("input[type='password']", CBVA_PASSWORD)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3_000)
            print("[auth] Logged in")
        except Exception as e:
            print(f"[auth] {e}")

        # Get tomorrow's tournaments (more likely to have data than today)
        for search_date in [TODAY, TOMORROW, "2026-06-06", "2026-06-07"]:
            raw = await fetch_trpc(page, "tournaments.search", {"date": search_date})
            result = json.loads(raw)
            data = result.get("result", {}).get("data", {}).get("json", {}).get("data", [])
            if data:
                print(f"\n=== tournaments.search date={search_date}: {len(data)} tournament(s) ===")
                for t in data[:3]:
                    divs = t.get("tournamentDivisions", [])
                    print(f"  id={t['id']} venue={t.get('venue',{}).get('name')} date={t['date']} divisions={len(divs)}")
                    for d in divs[:3]:
                        print(f"    div id={d.get('id')} name={d.get('name')} gender={d.get('gender')} divName={d.get('division',{}).get('name')}")
                break

        # Get full tournaments.get for a known upcoming tournament
        print("\n=== tournaments.get id=4704 ===")
        raw = await fetch_trpc(page, "tournaments.get", {"id": 4704})
        result = json.loads(raw).get("result", {}).get("data", {}).get("json", {})
        print(f"  name={result.get('name')} date={result.get('date')} venue={result.get('venue',{}).get('name')}")
        divs = result.get("tournamentDivisions", [])
        print(f"  divisions: {len(divs)}")
        for d in divs[:5]:
            print(f"    {d}")

        # Intercept calls on a tournament division page
        print("\n=== Intercepting division page calls ===")
        all_calls = []
        async def cap(resp):
            if "cbva.com/api/trpc" in resp.url:
                try:
                    body = await resp.text()
                    all_calls.append((resp.url, body[:2000]))
                except: pass
        page.on("response", cap)

        # Visit tournament 4633, division 16397 (tomorrow's Dockweiler tournament)
        await page.goto(f"{BASE_URL}/tournaments/4633/16397", wait_until="networkidle")
        await page.wait_for_timeout(3_000)
        print(f"Page text (first 500 chars): {(await page.evaluate('() => document.body.innerText'))[:500]}")
        for url, body in all_calls:
            print(f"\n  CALL: {url[:140]}")
            print(f"  BODY: {body}")
        all_calls.clear()

        # Try direct bracket/results endpoint guesses
        print("\n=== Direct bracket endpoint guesses ===")
        for ep, inp in [
            ("tournaments.getDivision",     {"id": 16397}),
            ("tournaments.getBracket",      {"divisionId": 16397}),
            ("tournamentDivisions.get",     {"id": 16397}),
            ("tournamentDivisions.getBracket", {"id": 16397}),
            ("brackets.get",               {"divisionId": 16397}),
        ]:
            try:
                raw = await fetch_trpc(page, ep, inp)
                snippet = raw[:300]
                if "NOT_FOUND" not in snippet:
                    print(f"  *** FOUND: {ep}: {snippet}")
                else:
                    print(f"  {ep}: not found")
            except Exception as ex:
                print(f"  {ep}: error {ex}")

        await browser.close()

asyncio.run(probe())
