#!/usr/bin/env python3
"""
Probe: intercept ALL tRPC calls on the Dockweiler Men's A division page
and attempt every plausible results/bracket/pool endpoint.

Division: 16405 (Men's A, Dockweiler, Los Angeles, June 7 2026)
Tournament: 4825
"""
import asyncio, json, os, urllib.parse
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")
DIV_ID        = 16405
T_ID          = 4825


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
        print("[auth] Done")

        # ── 1. Intercept all tRPC calls made by the division page ────────────
        print(f"\n=== Intercepting tRPC calls on /tournaments/{T_ID}/{DIV_ID} ===")
        intercepted = []

        async def capture(resp):
            if "/api/trpc/" in resp.url:
                intercepted.append(resp.url)

        page.on("response", capture)
        await page.goto(f"{BASE_URL}/tournaments/{T_ID}/{DIV_ID}", wait_until="networkidle")
        await page.wait_for_timeout(5_000)   # let any lazy-loaded requests fire
        page.remove_listener("response", capture)

        print(f"Captured {len(intercepted)} tRPC call(s):")
        for url in intercepted:
            ep = url.split("/api/trpc/")[1].split("?")[0]
            qs = urllib.parse.urlparse(url).query
            params = urllib.parse.parse_qs(qs)
            inp_raw = params.get("input", ["{}"])[0]
            try:
                inp = json.loads(inp_raw)
            except Exception:
                inp = inp_raw
            print(f"  {ep}  input={json.dumps(inp)[:200]}")

            # Re-fetch and print body
            try:
                inp_obj = inp.get("json", inp) if isinstance(inp, dict) else {}
                body = await fetch_trpc(page, ep, inp_obj)
                parsed = json.loads(body)
                data = parsed.get("result", {}).get("data", {}).get("json")
                print(f"    → {str(data)[:500]}")
            except Exception as ex:
                print(f"    → error: {ex}")

        # ── 2. Dump visible page text (scores shown in the UI?) ────────────
        print("\n=== Page text (first 3000 chars) ===")
        text = await page.evaluate("() => document.body.innerText")
        print(text[:3000])

        # ── 3. Try candidate endpoints we haven't tried yet ────────────────
        print(f"\n=== Brute-force endpoint candidates for div {DIV_ID} ===")
        candidates = [
            ("tournaments.getDivisionResults",  {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getDivisionBracket",  {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getDivisionSchedule", {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getStandings",        {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getPoolResults",      {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getPlayoffBracket",   {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getPlayoffs",         {"tournamentDivisionId": DIV_ID}),
            ("tournaments.getPoolPlay",         {"id": DIV_ID}),
            ("tournaments.getPool",             {"tournamentDivisionId": DIV_ID}),
            ("divisions.getResults",            {"id": DIV_ID}),
            ("divisions.getBracket",            {"id": DIV_ID}),
            ("divisions.getPools",              {"id": DIV_ID}),
            ("divisions.getSchedule",           {"tournamentDivisionId": DIV_ID}),
            ("pools.getByTournamentDivision",   {"tournamentDivisionId": DIV_ID}),
            ("pools.getResults",                {"tournamentDivisionId": DIV_ID}),
            ("matches.get",                     {"tournamentDivisionId": DIV_ID}),
            ("matches.getAll",                  {"tournamentDivisionId": DIV_ID}),
            ("matches.list",                    {"divisionId": DIV_ID}),
            ("games.get",                       {"tournamentDivisionId": DIV_ID}),
            ("scores.get",                      {"tournamentDivisionId": DIV_ID}),
        ]
        for ep, inp in candidates:
            body = await fetch_trpc(page, ep, inp)
            try:
                parsed = json.loads(body)
                data = parsed.get("result", {}).get("data", {}).get("json")
                err  = parsed.get("error")
                if data is not None and "NOT_FOUND" not in str(err) and "UNAUTHORIZED" not in str(err):
                    print(f"  *** HIT: {ep}\n    data={str(data)[:600]}")
                else:
                    msg = (parsed.get("error") or {}).get("json", {}).get("message", "?")
                    print(f"  {ep}: {msg}")
            except Exception as ex:
                print(f"  {ep}: parse error — {ex}")

        await browser.close()


asyncio.run(probe())
