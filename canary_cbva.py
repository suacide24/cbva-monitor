#!/usr/bin/env python3
"""
CBVA contract canary.

The monitor derives registration status two ways:
  • _reg_status_from_division — capacity numbers from the tRPC API (primary)
  • _parse_reg_status         — button-text scrape (fallback)

On a registerable division (open / waitlist / waitlist_full) these MUST agree.
Persistent disagreement means CBVA renamed its buttons or changed its API
shape — i.e. the monitor's status detection is about to silently break. This
canary samples live division pages, compares the two methods, and fails (with
an alert email) when the contract no longer holds.

Run locally:   python canary_cbva.py
CI:            .github/workflows/canary.yml (scheduled daily)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright

import monitor

SAMPLE_TARGET    = 16     # how many division pages to inspect
MIN_REGISTERABLE = 4      # need at least this many to judge text-vs-API agreement
MIN_AGREEMENT    = 0.80   # API and text must agree on >= this fraction
MAX_API_NONE     = 0.50   # if more than this fraction lack capacity data → API broke

REGISTERABLE = {"open", "waitlist", "waitlist_full"}


async def _collect(pg) -> list[dict]:
    tours = await monitor._fetch_upcoming_tournaments(pg, weeks=3)
    now_utc = datetime.now(timezone.utc)
    samples: list[dict] = []
    for t in tours:
        tid = t.get("id")
        full = await monitor._trpc_get(pg, "tournaments.get", {"id": tid})
        if not isinstance(full, dict):
            continue
        reg_open = monitor._parse_iso(full.get("registrationOpenAt")
                                      or full.get("registrationOpenDate"))
        for d in (full.get("tournamentDivisions") or []):
            did = d.get("id")
            if not did:
                continue
            url = f"https://cbva.com/tournaments/{tid}/{did}"
            try:
                await pg.goto(url, wait_until="networkidle", timeout=35000)
                text = await pg.evaluate("() => document.body.innerText")
            except Exception:
                continue
            samples.append({
                "url":  url,
                "api":  monitor._reg_status_from_division(d, reg_open, now_utc),
                "text": monitor._parse_reg_status(text),
            })
            if len(samples) >= SAMPLE_TARGET:
                return samples
    return samples


def evaluate(samples: list[dict]) -> tuple[bool, list[str]]:
    """Return (ok, report_lines)."""
    lines: list[str] = []
    n = len(samples)
    lines.append(f"Sampled {n} live division page(s).")
    if n == 0:
        lines.append("INCONCLUSIVE: no pages sampled (CBVA unreachable?) — not failing.")
        return True, lines

    api_none = [s for s in samples if s["api"] is None]
    none_rate = len(api_none) / n
    lines.append(f"API capacity data present: {n - len(api_none)}/{n} "
                 f"(missing {none_rate:.0%}).")

    registerable = [s for s in samples if s["api"] in REGISTERABLE]
    agree = [s for s in registerable if s["api"] == s["text"]]
    lines.append(f"Registerable divisions (API): {len(registerable)}; "
                 f"API↔text agreement: {len(agree)}/{len(registerable)}.")

    disagreements = [s for s in registerable if s["api"] != s["text"]]
    for s in disagreements:
        lines.append(f"  ✗ {s['url']}  api={s['api']}  text={s['text']}")

    ok = True
    if none_rate > MAX_API_NONE:
        ok = False
        lines.append(f"FAIL: capacity data missing on {none_rate:.0%} of divisions "
                     f"(> {MAX_API_NONE:.0%}) — tRPC division shape may have changed.")

    if len(registerable) >= MIN_REGISTERABLE:
        rate = len(agree) / len(registerable)
        if rate < MIN_AGREEMENT:
            ok = False
            lines.append(f"FAIL: API↔text agreement {rate:.0%} < {MIN_AGREEMENT:.0%} "
                         f"— CBVA likely renamed its registration buttons "
                         f"(_parse_reg_status fallback is broken).")
    else:
        lines.append(f"Note: only {len(registerable)} registerable divisions "
                     f"(< {MIN_REGISTERABLE}); text-parse contract not judged this run.")

    lines.append("OK: status-detection contract holds." if ok else "CONTRACT VIOLATED.")
    return ok, lines


def _alert(report: str) -> None:
    if not (monitor.EMAIL_FROM and monitor.EMAIL_PASS):
        print("[canary] No email creds — skipping alert.")
        return
    html = (f"<h2 style='color:#cc3333'>🚨 CBVA Contract Canary failed</h2>"
            f"<p>The monitor's registration-status detection may be broken.</p>"
            f"<pre style='background:#f5f5f5;padding:12px;border-radius:6px;"
            f"white-space:pre-wrap;font-size:13px'>{report}</pre>"
            f"<p style='font-size:12px;color:#888'>"
            f"<a href='https://github.com/suacide24/cbva-monitor/actions'>View Actions →</a></p>")
    monitor.send_email("🚨 CBVA Contract Canary failed", html, to=monitor.EMAIL_FROM)


async def main() -> int:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        try:
            samples = await _collect(pg)
        finally:
            await b.close()
    ok, lines = evaluate(samples)
    report = "\n".join(lines)
    print(report)
    if not ok:
        _alert(report)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
