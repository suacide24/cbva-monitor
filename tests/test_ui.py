#!/usr/bin/env python3
"""
UI / front-end regression tests for index.html.

These drive the *actual* index.html in a headless Chromium browser, with the
GitHub API stubbed out by local fixture data (tests/fixtures/). They lock down
the browser-side behaviour that plain Python unit tests can't reach — the
exact class of bugs that have bitten this project:

  * tournament_urls migrating from "str" → {url, nickname}
  * gender / division toggles persisting into the saved payload
  * the "unsaved changes" save-bar appearing on edit and clearing on save
  * the save serialization (trimming, omitting empty sections)

Requires Playwright + Chromium:
    pip install playwright && python -m playwright install chromium

Run:
    python -m unittest tests.test_ui -v      (or)
    python tests/test_ui.py
"""
from __future__ import annotations

import base64
import copy
import functools
import http.server
import json
import os
import re
import socketserver
import threading
import unittest

from playwright.sync_api import sync_playwright

ROOT          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

with open(os.path.join(FIXTURES, "sample_users.json"))   as f: SAMPLE_USERS   = json.load(f)
with open(os.path.join(FIXTURES, "sample_players.json")) as f: SAMPLE_PLAYERS = json.load(f)

# Matches both the GET (load) and PUT (save) calls to users.json.
RE_CONTENTS = re.compile(r"api\.github\.com/.+/contents/users\.json")
RE_PLAYERS  = re.compile(r"raw\.githubusercontent\.com/.+/players\.json")


class _Server:
    """Serves the cbva-monitor directory on a random localhost port."""
    def __enter__(self):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
        handler.log_message = lambda *a, **k: None  # silence
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port  = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class UITestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server  = _Server().__enter__()
        cls.pw      = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.__exit__()

    def setUp(self):
        # Per-test mutable state the route handlers read at request time.
        self.users_data    = copy.deepcopy(SAMPLE_USERS)
        self.players_data  = copy.deepcopy(SAMPLE_PLAYERS)
        self.players_ok    = True          # set False to simulate players.json missing
        self.saved_payloads = []           # decoded JSON of every PUT (save)

        self.context = self.browser.new_context()
        # Authenticate before any page script runs so we skip the password gate.
        self.context.add_init_script("window.localStorage.setItem('cbva_authed','1')")
        self.page = self.context.new_page()
        self._install_routes(self.page)

    def tearDown(self):
        self.context.close()

    # ── request stubbing ──────────────────────────────────────────────
    def _install_routes(self, page):
        def contents(route):
            req = route.request
            if req.method == "PUT":
                payload = req.post_data_json
                decoded = json.loads(base64.b64decode(payload["content"]).decode())
                self.saved_payloads.append(decoded)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"content": {"sha": "newsha"}}))
            else:  # GET
                b64 = base64.b64encode(json.dumps(self.users_data).encode()).decode()
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"sha": "testsha", "content": b64}))

        def players(route):
            if self.players_ok:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(self.players_data))
            else:
                route.fulfill(status=404, body="not found")

        page.route(RE_CONTENTS, contents)
        page.route(RE_PLAYERS, players)

    # ── helpers ───────────────────────────────────────────────────────
    def load(self):
        self.page.goto(f"http://127.0.0.1:{self.server.port}/index.html")
        self.page.wait_for_function("_loaded === true")

    def save(self):
        self.page.click("#save-btn")
        # Wait until the save round-trip records a payload.
        self.page.wait_for_function("document.getElementById('save-confirm').textContent === 'Saved ✓'")

    def last_saved(self):
        self.assertTrue(self.saved_payloads, "no save payload was captured")
        return self.saved_payloads[-1]

    def user_in(self, payload, email):
        for u in payload:
            if u["email"] == email:
                return u
        self.fail(f"{email} not found in saved payload")


# ── Auth gate ─────────────────────────────────────────────────────────────
class TestAuthGate(UITestBase):
    def test_unauthed_shows_password_gate(self):
        # Override the pre-auth init script: clear the flag instead.
        self.context.add_init_script("window.localStorage.removeItem('cbva_authed')")
        page = self.context.new_page()
        self._install_routes(page)
        page.goto(f"http://127.0.0.1:{self.server.port}/index.html")
        self.assertEqual(page.locator("#setup").evaluate("e => getComputedStyle(e).display"), "block")
        self.assertEqual(page.locator("#app-body").evaluate("e => getComputedStyle(e).display"), "none")

    def test_correct_password_enters_app(self):
        self.context.add_init_script("window.localStorage.removeItem('cbva_authed')")
        page = self.context.new_page()
        self._install_routes(page)
        page.goto(f"http://127.0.0.1:{self.server.port}/index.html")
        page.fill("#pw-input", "stalker")
        page.click(".btn-enter")
        page.wait_for_function("_loaded === true")
        self.assertEqual(page.locator("#app-body").evaluate("e => getComputedStyle(e).display"), "block")


# ── Load + render ──────────────────────────────────────────────────────────
class TestLoadAndRender(UITestBase):
    def test_both_panels_render_one_card_per_user(self):
        self.load()
        self.assertEqual(self.page.locator("#cards-players .user-card").count(), 2)
        self.assertEqual(self.page.locator("#cards-tournament .user-card").count(), 2)

    def test_player_chips_render(self):
        self.load()
        chips = self.page.locator("#cards-players .user-card").first.locator(".chip")
        self.assertEqual(chips.count(), 2)

    def test_unknown_player_marked_unverified(self):
        self.load()
        card = self.page.locator("#cards-players .user-card").first
        # "Phantom Player" is not in sample_players.json → unverified
        self.assertEqual(card.locator(".chip.unverified").count(), 1)
        self.assertIn("Phantom Player", card.locator(".chip.unverified").inner_text())

    def test_no_players_file_treats_all_as_known(self):
        self.players_ok = False
        self.load()
        # verifiedPlayers stays empty → nothing is flagged unverified
        self.assertEqual(self.page.locator("#cards-players .chip.unverified").count(), 0)


# ── Tab switching ──────────────────────────────────────────────────────────
class TestTabs(UITestBase):
    def test_default_tab_is_players(self):
        self.load()
        self.assertTrue(self.page.locator("#panel-players").evaluate("e => e.classList.contains('active')"))
        self.assertFalse(self.page.locator("#panel-tournament").evaluate("e => e.classList.contains('active')"))

    def test_switch_to_tournament_tab(self):
        self.load()
        self.page.click("#tab-tournament")
        self.assertTrue(self.page.locator("#panel-tournament").evaluate("e => e.classList.contains('active')"))
        self.assertFalse(self.page.locator("#panel-players").evaluate("e => e.classList.contains('active')"))


# ── Player Tracker ─────────────────────────────────────────────────────────
class TestPlayerTracker(UITestBase):
    def test_add_player_via_enter(self):
        self.load()
        card = self.page.locator("#cards-players .user-card").first
        card.locator(".add-player-input").fill("Tri Bourne")
        card.locator(".add-player-input").press("Enter")
        self.assertEqual(card.locator(".chip").count(), 3)
        self.save()
        self.assertIn("Tri Bourne", self.user_in(self.last_saved(), "alice@example.com")["players"])

    def test_remove_player(self):
        self.load()
        card = self.page.locator("#cards-players .user-card").first
        card.locator(".chip").first.locator(".chip-del").click()
        self.assertEqual(card.locator(".chip").count(), 1)
        self.save()
        self.assertNotIn("Ramon Sua", self.user_in(self.last_saved(), "alice@example.com")["players"])

    def test_duplicate_player_not_added_twice(self):
        self.load()
        card = self.page.locator("#cards-players .user-card").first
        card.locator(".add-player-input").fill("Ramon Sua")
        card.locator(".add-player-input").press("Enter")
        self.assertEqual(card.locator(".chip").count(), 2)


# ── Tournament Notifier: city watches (gender/division persistence) ─────────
class TestCityWatches(UITestBase):
    def _first_watch_card(self):
        self.load()
        self.page.click("#tab-tournament")
        return self.page.locator("#cards-tournament .user-card").first

    def test_saved_toggles_reflected_on_load(self):
        card = self._first_watch_card()
        on = card.locator(".toggle-chip.on")
        labels = [on.nth(i).inner_text() for i in range(on.count())]
        self.assertCountEqual(labels, ["Men's", "B", "A"])

    def test_toggle_gender_persists_to_save(self):
        card = self._first_watch_card()
        card.get_by_role("button", name="Women's").click()
        self.save()
        watch = self.user_in(self.last_saved(), "alice@example.com")["tournament_watches"][0]
        self.assertIn("Women's", watch["genders"])
        self.assertIn("Men's", watch["genders"])  # original kept

    def test_toggle_division_off_persists_to_save(self):
        card = self._first_watch_card()
        card.get_by_role("button", name="B", exact=True).click()  # turn OFF
        self.save()
        watch = self.user_in(self.last_saved(), "alice@example.com")["tournament_watches"][0]
        self.assertNotIn("B", watch["divisions"])
        self.assertIn("A", watch["divisions"])

    def test_add_city(self):
        card = self._first_watch_card()
        card.get_by_role("button", name="+ Add city").click()
        self.assertEqual(card.locator(".city-watch-row").count(), 2)

    def test_remove_city(self):
        card = self._first_watch_card()
        card.locator(".city-watch-row").first.locator(".del-watch").click()
        self.assertEqual(card.locator(".city-watch-row").count(), 0)


# ── Tournament Notifier: signup status URL watches ──────────────────────────
class TestUrlWatches(UITestBase):
    def _tournament_card(self):
        self.load()
        self.page.click("#tab-tournament")
        return self.page.locator("#cards-tournament .user-card").first

    def test_dict_url_renders_nickname_and_link(self):
        card = self._tournament_card()
        chip = card.locator(".url-chip").first
        self.assertEqual(chip.locator(".url-nick").input_value(), "OB Open")
        self.assertIn("/tournaments/4668/16522", chip.locator(".url-href").inner_text())

    def test_legacy_string_url_migrates(self):
        # A pre-migration entry stored as a bare string must still render.
        self.users_data[0]["tournament_urls"] = ["https://cbva.com/tournaments/999/111"]
        card = self._tournament_card()
        chip = card.locator(".url-chip").first
        self.assertEqual(chip.locator(".url-nick").input_value(), "")          # empty nickname
        self.assertIn("/tournaments/999/111", chip.locator(".url-href").inner_text())
        # And it saves back in the new object form.
        chip.locator(".url-nick").fill("Migrated")
        self.save()
        urls = self.user_in(self.last_saved(), "alice@example.com")["tournament_urls"]
        self.assertEqual(urls[0], {"url": "https://cbva.com/tournaments/999/111", "nickname": "Migrated"})

    def test_add_url_with_nickname(self):
        card = self._tournament_card()
        form = card.locator(".add-url-form")
        form.locator(".add-url-input").first.fill("https://cbva.com/tournaments/777/222")
        form.locator(".add-url-input").nth(1).fill("New Watch")
        card.get_by_role("button", name="Add", exact=True).click()
        self.save()
        urls = self.user_in(self.last_saved(), "alice@example.com")["tournament_urls"]
        self.assertIn({"url": "https://cbva.com/tournaments/777/222", "nickname": "New Watch"}, urls)

    def test_remove_url(self):
        card = self._tournament_card()
        card.locator(".url-chip").first.locator(".url-del").click()
        self.assertEqual(card.locator(".url-chip").count(), 0)


# ── Save-bar / dirty tracking ───────────────────────────────────────────────
class TestSaveBar(UITestBase):
    def test_hidden_on_fresh_load(self):
        self.load()
        self.assertFalse(self.page.locator("#save-bar").evaluate("e => e.classList.contains('show')"))
        self.assertFalse(self.page.evaluate("_dirty"))

    def test_appears_after_toggle(self):
        self.load()
        self.page.click("#tab-tournament")
        self.page.locator("#cards-tournament .toggle-chip").first.click()
        self.assertTrue(self.page.locator("#save-bar").evaluate("e => e.classList.contains('show')"))
        self.assertTrue(self.page.evaluate("_dirty"))

    def test_appears_after_nickname_edit(self):
        self.load()
        self.page.click("#tab-tournament")
        self.page.locator("#cards-tournament .url-nick").first.fill("Edited")
        self.assertTrue(self.page.evaluate("_dirty"))

    def test_clears_after_save(self):
        self.load()
        self.page.click("#tab-tournament")
        self.page.locator("#cards-tournament .toggle-chip").first.click()
        self.save()
        self.assertFalse(self.page.locator("#save-bar").evaluate("e => e.classList.contains('show')"))
        self.assertFalse(self.page.evaluate("_dirty"))


# ── Save serialization ──────────────────────────────────────────────────────
class TestSaveSerialization(UITestBase):
    def test_empty_sections_omitted(self):
        self.load()
        # bob has no watches/urls → those keys should be absent
        self.page.click("#save-btn")
        self.page.wait_for_function("document.getElementById('save-confirm').textContent === 'Saved ✓'")
        bob = self.user_in(self.last_saved(), "bob@example.com")
        self.assertNotIn("tournament_watches", bob)
        self.assertNotIn("tournament_urls", bob)
        self.assertEqual(bob["players"], ["Faizan Zubair"])

    def test_populated_sections_included(self):
        self.load()
        self.page.click("#save-btn")
        self.page.wait_for_function("document.getElementById('save-confirm').textContent === 'Saved ✓'")
        alice = self.user_in(self.last_saved(), "alice@example.com")
        self.assertEqual(len(alice["tournament_watches"]), 1)
        self.assertEqual(len(alice["tournament_urls"]), 1)

    def test_add_and_remove_subscriber(self):
        self.load()
        self.page.locator("#panel-players .btn-add-user").click()
        self.assertEqual(self.page.locator("#cards-players .user-card").count(), 3)
        # Fill the new (empty) email then remove the first
        self.page.locator("#cards-players .user-card").last.locator(".email-input").fill("carol@example.com")
        self.page.locator("#cards-players .user-card").first.locator(".del-user").click()
        self.save()
        emails = [u["email"] for u in self.last_saved()]
        self.assertIn("carol@example.com", emails)
        self.assertNotIn("alice@example.com", emails)


if __name__ == "__main__":
    unittest.main(verbosity=2)
