#!/usr/bin/env python3
"""
One-off script to discover CBVA's tRPC endpoints for tournaments.
Visits /tournaments and a specific tournament page, logs all API calls.
"""
import asyncio, json, os
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
BASE_URL      = "https://cbva.com"
CBVA_EMAIL    = os.environ.get("CBVA_EMAIL", "")
CBVA_PASSWORD = os.environ.get("CBVA_PASSWORD", "")

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
            print(f"[auth] Logged in")
        except Exception as e:
            print(f"[auth] {e}")

        calls = []
        async def capture(response):
            if "cbva.com/api/trpc" in response.url:
                try:
                    body = await response.text()
                    calls.append((response.url, body[:2000]))
                except:
                    pass
        page.on("response", capture)

        # Visit tournaments listing
        print("\n=== /tournaments ===")
        await page.goto(f"{BASE_URL}/tournaments", wait_until="networkidle")
        await page.wait_for_timeout(2_000)
        for url, body in calls:
            print(f"URL: {url}")
            print(f"BODY: {body}\n")
        calls.clear()

        # Visit upcoming tournaments page
        print("\n=== /tournaments/upcoming ===")
        await page.goto(f"{BASE_URL}/tournaments/upcoming", wait_until="networkidle")
        await page.wait_for_timeout(2_000)
        for url, body in calls:
            print(f"URL: {url}")
            print(f"BODY: {body}\n")
        calls.clear()

        # Find a tournament link and follow it
        links = await page.query_selector_all("a[href*='/tournaments/']")
        if links:
            href = await links[0].get_attribute("href")
            print(f"\n=== First tournament link: {href} ===")
            await page.goto(f"{BASE_URL}{href}" if href.startswith("/") else href, wait_until="networkidle")
            await page.wait_for_timeout(2_000)
            for url, body in calls:
                print(f"URL: {url}")
                print(f"BODY: {body}\n")
            calls.clear()

        await browser.close()

asyncio.run(probe())
