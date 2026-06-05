#!/usr/bin/env python3
"""
Probe CBVA tournament APIs — tries known tRPC endpoints and reads page content.
"""
import asyncio, json, os, urllib.parse
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")
TODAY         = datetime.now().strftime("%Y-%m-%d")

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

        # Login first
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

        # Intercept all tRPC calls
        all_calls = []
        async def capture(response):
            if "cbva.com/api/trpc" in response.url:
                try:
                    body = await response.text()
                    all_calls.append((response.url, body[:3000]))
                except: pass
        page.on("response", capture)

        # Navigate to tournaments page with longer wait
        print(f"\n=== Visiting /tournaments (today={TODAY}) ===")
        await page.goto(f"{BASE_URL}/tournaments", wait_until="networkidle")
        await page.wait_for_timeout(5_000)
        print(f"Page text preview: {(await page.evaluate('() => document.body.innerText'))[:300]}")
        for url, body in all_calls:
            print(f"  CALL: {url[:120]}")
            print(f"  BODY: {body[:500]}\n")
        all_calls.clear()

        # Try direct tRPC endpoint calls
        print("\n=== Direct tRPC probe ===")
        endpoints_to_try = [
            ("tournaments.list",       {"date": TODAY}),
            ("tournaments.list",       {}),
            ("tournaments.upcoming",   {}),
            ("tournaments.search",     {"date": TODAY}),
            ("tournaments.getByDate",  {"date": TODAY}),
            ("tournaments.today",      {}),
        ]
        for ep, inp in endpoints_to_try:
            try:
                raw = await fetch_trpc(page, ep, inp)
                print(f"  {ep}({inp}): {raw[:400]}")
            except Exception as ex:
                print(f"  {ep}: ERROR {ex}")

        # Try fetching tournament ID 4704 (the one we know about)
        print("\n=== Known tournament ID 4704 ===")
        raw = await fetch_trpc(page, "tournaments.get", {"id": 4704})
        print(raw[:2000])

        # Visit the tournaments upcoming page and read links
        print("\n=== Page links on /tournaments ===")
        await page.goto(f"{BASE_URL}/tournaments", wait_until="networkidle")
        await page.wait_for_timeout(5_000)
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]'))
                       .map(a => a.href)
                       .filter(h => h.includes('/tournaments/') && !h.includes('javascript'))
                       .slice(0, 20)
        """)
        print(f"Tournament links: {links}")

        await browser.close()

asyncio.run(probe())
