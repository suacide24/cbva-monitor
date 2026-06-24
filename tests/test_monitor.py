"""
Unit tests for monitor.py pure-Python helpers.

Runs without Playwright, CBVA credentials, or network access.
Covers: name matching, division labels, team/player extraction,
        score formatting, bracket math, and outcome detection.
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub out playwright so monitor can be imported without a browser install ──
for mod in ("playwright", "playwright.async_api", "dotenv"):
    sys.modules.setdefault(mod, MagicMock())

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor  # noqa: E402  (must come after stubs)


# ── _names_match ──────────────────────────────────────────────────────────────

class TestNamesMatch(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(monitor._names_match("Ramon Sua", "Ramon Sua"))

    def test_case_insensitive(self):
        self.assertTrue(monitor._names_match("RAMON SUA", "ramon sua"))

    def test_leading_trailing_whitespace(self):
        self.assertTrue(monitor._names_match("  Ramon Sua  ", "Ramon Sua"))

    def test_last_name_plus_first_two_chars(self):
        # "Ra" matches "Ra", "Sua" == "Sua" → match
        self.assertTrue(monitor._names_match("Ramon Sua", "Rachel Sua"))

    def test_different_last_name(self):
        self.assertFalse(monitor._names_match("Ramon Smith", "Ramon Sua"))

    def test_different_first_two_chars(self):
        # "Jo" ≠ "Ra" → no match even with same last name
        self.assertFalse(monitor._names_match("John Sua", "Ramon Sua"))

    def test_single_word_no_match(self):
        self.assertFalse(monitor._names_match("Ramon", "Sua"))

    def test_single_word_exact(self):
        self.assertTrue(monitor._names_match("Ramon", "Ramon"))


# ── _division_label ───────────────────────────────────────────────────────────

class TestDivisionLabel(unittest.TestCase):

    def _div(self, display, gender="male", max_age=None):
        return {"division": {"display": display}, "gender": gender, "maxAge": max_age}

    def test_mens_open(self):
        self.assertEqual(monitor._division_label(self._div("Open")), "Men's OPEN")

    def test_womens_aa(self):
        self.assertEqual(monitor._division_label(self._div("AA", gender="female")), "Women's AA")

    def test_boys_with_age(self):
        self.assertEqual(monitor._division_label(self._div("B", gender="male", max_age=16)), "Boys B")

    def test_girls_with_age(self):
        self.assertEqual(monitor._division_label(self._div("A", gender="female", max_age=18)), "Girls A")

    def test_coed(self):
        self.assertEqual(monitor._division_label(self._div("BB", gender="coed")), "Coed BB")

    def test_malformed_no_level(self):
        # Empty dict → default gender "male", empty level → "Men's" (no level to strip)
        self.assertEqual(monitor._division_label({}), "Men's")


# ── _extract_teams ────────────────────────────────────────────────────────────

class TestExtractTeams(unittest.TestCase):

    def test_direct_list(self):
        teams = [{"id": 1}, {"id": 2}]
        self.assertEqual(monitor._extract_teams(teams), teams)

    def test_dict_with_teams_key(self):
        teams = [{"id": 1}]
        self.assertEqual(monitor._extract_teams({"teams": teams}), teams)

    def test_dict_with_entries_key(self):
        entries = [{"id": 9}]
        self.assertEqual(monitor._extract_teams({"entries": entries}), entries)

    def test_dict_with_registrations_key(self):
        regs = [{"id": 7}]
        self.assertEqual(monitor._extract_teams({"registrations": regs}), regs)

    def test_unknown_shape_returns_empty(self):
        self.assertEqual(monitor._extract_teams({"something": []}), [])

    def test_none_returns_empty(self):
        self.assertEqual(monitor._extract_teams(None), [])


# ── _extract_players ──────────────────────────────────────────────────────────

class TestExtractPlayers(unittest.TestCase):

    def test_profiles_key_direct(self):
        ps = [{"firstName": "Ramon"}]
        self.assertEqual(monitor._extract_players({"profiles": ps}), ps)

    def test_players_key_direct(self):
        ps = [{"firstName": "Selina"}]
        self.assertEqual(monitor._extract_players({"players": ps}), ps)

    def test_nested_under_team(self):
        ps = [{"firstName": "Kyle"}]
        self.assertEqual(monitor._extract_players({"team": {"profiles": ps}}), ps)

    def test_nested_players_key(self):
        ps = [{"firstName": "Ana"}]
        self.assertEqual(monitor._extract_players({"team": {"players": ps}}), ps)

    def test_empty_team(self):
        self.assertEqual(monitor._extract_players({}), [])

    def test_non_list_value_ignored(self):
        self.assertEqual(monitor._extract_players({"profiles": "bad"}), [])


# ── _ordinal ──────────────────────────────────────────────────────────────────

class TestOrdinal(unittest.TestCase):

    def test_1st(self):  self.assertEqual(monitor._ordinal(1),  "1st")
    def test_2nd(self):  self.assertEqual(monitor._ordinal(2),  "2nd")
    def test_3rd(self):  self.assertEqual(monitor._ordinal(3),  "3rd")
    def test_4th(self):  self.assertEqual(monitor._ordinal(4),  "4th")
    def test_11th(self): self.assertEqual(monitor._ordinal(11), "11th")
    def test_12th(self): self.assertEqual(monitor._ordinal(12), "12th")
    def test_13th(self): self.assertEqual(monitor._ordinal(13), "13th")
    def test_21st(self): self.assertEqual(monitor._ordinal(21), "21st")
    def test_22nd(self): self.assertEqual(monitor._ordinal(22), "22nd")


# ── _score_str ────────────────────────────────────────────────────────────────

class TestScoreStr(unittest.TestCase):

    def _set(self, a, b, status=None):
        s = {"teamAScore": a, "teamBScore": b}
        if status:
            s["status"] = status
        return s

    def test_single_set(self):
        self.assertEqual(monitor._score_str([self._set(21, 15)]), "21-15")

    def test_two_sets(self):
        self.assertEqual(monitor._score_str([self._set(21, 18), self._set(21, 14)]), "21-18, 21-14")

    def test_not_started_skipped(self):
        self.assertEqual(monitor._score_str([self._set(0, 0, "not_started")]), "not started")

    def test_empty_list(self):
        self.assertEqual(monitor._score_str([]), "not started")

    def test_none(self):
        self.assertEqual(monitor._score_str(None), "not started")

    def test_real_zeros_skipped_when_status_none(self):
        # 0-0 with status=None is treated as not_started (matches CBVA API behavior)
        self.assertEqual(monitor._score_str([self._set(0, 0)]), "not started")

    def test_real_zeros_shown_with_completed_status(self):
        # 0-0 with an explicit non-not_started status does render
        self.assertEqual(monitor._score_str([self._set(0, 0, "completed")]), "0-0")


# ── _build_seed_map ───────────────────────────────────────────────────────────

class TestBuildSeedMap(unittest.TestCase):

    def test_basic(self):
        matches = [
            {"teamAId": 10, "teamASeed": 1, "teamBId": 20, "teamBSeed": 8},
            {"teamAId": 30, "teamASeed": 4, "teamBId": 40, "teamBSeed": 5},
        ]
        sm = monitor._build_seed_map(matches)
        self.assertEqual(sm[10], 1)
        self.assertEqual(sm[20], 8)
        self.assertEqual(sm[30], 4)
        self.assertEqual(sm[40], 5)

    def test_null_seeds_skipped(self):
        matches = [{"teamAId": 10, "teamASeed": None, "teamBId": 20, "teamBSeed": 3}]
        sm = monitor._build_seed_map(matches)
        self.assertNotIn(10, sm)
        self.assertEqual(sm[20], 3)

    def test_empty(self):
        self.assertEqual(monitor._build_seed_map([]), {})


# ── _finish_place ─────────────────────────────────────────────────────────────

class TestFinishPlace(unittest.TestCase):

    def _matches(self, round_counts: dict[int, int]) -> list[dict]:
        """Build a fake match list: {round_num: team_count_in_round}."""
        matches = []
        for r, n in round_counts.items():
            for _ in range(n):
                matches.append({"round": r})
        return matches

    def test_lost_in_final_is_2nd(self):
        # Final has 1 match; loser is 2nd
        matches = self._matches({0: 8, 1: 4, 2: 2, 3: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=3), "2nd")

    def test_lost_in_semis_is_3rd_4th(self):
        matches = self._matches({0: 8, 1: 4, 2: 2, 3: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=2), "3rd–4th")

    def test_lost_in_quarters_is_5th_8th(self):
        matches = self._matches({0: 8, 1: 4, 2: 2, 3: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=1), "5th–8th")

    def test_lost_in_r1_of_32_team(self):
        # R0: 16 matches, R1: 8, R2: 4, R3: 2, R4: 1
        matches = self._matches({0: 16, 1: 8, 2: 4, 3: 2, 4: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=0), "17th–32nd")


# ── _player_outcome ───────────────────────────────────────────────────────────

class TestPlayerOutcome(unittest.TestCase):

    def _match(self, round_num, a_id, b_id, winner_id, a_seed=None, b_seed=None):
        return {
            "round": round_num,
            "teamAId": a_id, "teamBId": b_id,
            "teamASeed": a_seed, "teamBSeed": b_seed,
            "winnerId": winner_id,
            "status": "completed",
            "sets": [],
        }

    def test_champion(self):
        # Wins R0 and R1 (final), R1 is max_round → champion
        matches = [
            self._match(0, a_id=1, b_id=2, winner_id=1),
            self._match(1, a_id=1, b_id=3, winner_id=1),
        ]
        out = monitor._player_outcome(matches, {"team_id": 1})
        self.assertTrue(out["is_champion"])
        self.assertIsNone(out["finish_round"])

    def test_lost_in_round_0(self):
        matches = [self._match(0, a_id=1, b_id=2, winner_id=2)]
        out = monitor._player_outcome(matches, {"team_id": 1})
        self.assertFalse(out["is_champion"])
        self.assertEqual(out["finish_round"], 0)

    def test_won_then_lost(self):
        matches = [
            self._match(0, a_id=1, b_id=2, winner_id=1),
            self._match(1, a_id=1, b_id=3, winner_id=3),
        ]
        out = monitor._player_outcome(matches, {"team_id": 1})
        self.assertFalse(out["is_champion"])
        self.assertEqual(out["finish_round"], 1)
        self.assertFalse(out["last_won"])

    def test_empty_data_returns_empty(self):
        self.assertEqual(monitor._player_outcome([], {"team_id": 1}), {})

    def test_match_by_seed(self):
        matches = [self._match(0, a_id=10, b_id=20, winner_id=20, a_seed=1, b_seed=8)]
        out = monitor._player_outcome(matches, {"team_seed": 1})
        self.assertFalse(out["is_champion"])
        self.assertEqual(out["finish_round"], 0)

    def test_skips_non_completed_matches(self):
        m = self._match(0, a_id=1, b_id=2, winner_id=1)
        m["status"] = "in_progress"
        out = monitor._player_outcome([m], {"team_id": 1})
        # Non-completed matches don't affect win/loss tracking
        self.assertFalse(out["is_champion"])
        self.assertIsNone(out["finish_round"])
        self.assertIsNone(out["last_round"])


# ── load_users ────────────────────────────────────────────────────────────────

class TestLoadUsers(unittest.TestCase):

    def _write_users(self, data):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir="."
        )
        json.dump(data, tmp)
        tmp.close()
        return tmp.name

    def test_valid_users(self):
        path = self._write_users([
            {"email": "a@b.com", "players": ["Player One"]},
            {"email": "c@d.com", "players": ["Player Two", "Player Three"]},
        ])
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = path
        try:
            users = monitor.load_users()
            self.assertEqual(len(users), 2)
            self.assertEqual(users[0]["email"], "a@b.com")
        finally:
            monitor.USERS_FILE = orig
            Path(path).unlink(missing_ok=True)

    def test_filters_entries_without_email(self):
        path = self._write_users([
            {"email": "", "players": ["X"]},
            {"email": "ok@test.com", "players": ["Y"]},
        ])
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = path
        try:
            users = monitor.load_users()
            self.assertEqual(len(users), 1)
        finally:
            monitor.USERS_FILE = orig
            Path(path).unlink(missing_ok=True)

    def test_filters_entries_without_players(self):
        path = self._write_users([
            {"email": "ok@test.com", "players": []},
            {"email": "ok2@test.com", "players": ["Y"]},
        ])
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = path
        try:
            users = monitor.load_users()
            self.assertEqual(len(users), 1)
        finally:
            monitor.USERS_FILE = orig
            Path(path).unlink(missing_ok=True)

    def test_missing_file_returns_empty(self):
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = "/tmp/does_not_exist_cbva.json"
        try:
            users = monitor.load_users()
            self.assertEqual(users, [])
        finally:
            monitor.USERS_FILE = orig


# ── Regression: PLAYER_NAMES bug ──────────────────────────────────────────────

class TestPlayerNamesBugRegression(unittest.TestCase):
    """
    The scan phase crashed with `NameError: name 'PLAYER_NAMES' is not defined`.
    The fix: line 432 of monitor.py must reference the local `player_names`
    parameter, not a non-existent module-level `PLAYER_NAMES`.
    """

    def test_PLAYER_NAMES_not_in_module(self):
        self.assertFalse(
            hasattr(monitor, "PLAYER_NAMES"),
            "PLAYER_NAMES must not exist as a module-level name — "
            "scan_today_tournaments uses its `player_names` parameter instead",
        )

    def test_player_names_param_used_in_scan(self):
        import inspect
        src = inspect.getsource(monitor.scan_today_tournaments)
        self.assertNotIn(
            "PLAYER_NAMES", src,
            "scan_today_tournaments must not reference PLAYER_NAMES",
        )
        self.assertIn(
            "player_names", src,
            "scan_today_tournaments must use its player_names parameter",
        )


# ── Regression: false playoff email ──────────────────────────────────────────

class TestPlayoffGate(unittest.TestCase):
    """
    CBVA publishes the complete bracket at tournament start with every match
    in "scheduled" status before play begins.  The playoff email must NOT fire
    just because the player's seed appears in the bracket — it must wait until
    at least one of their matches has moved past "scheduled"/"not_started".
    """

    def _scheduled_match(self, a_seed, b_seed):
        return {
            "teamASeed": a_seed, "teamBSeed": b_seed,
            "teamAId": None, "teamBId": None,
            "status": "scheduled",
            "winnerId": None, "sets": [],
        }

    def _active_match(self, a_seed, b_seed, status="in_progress"):
        return {
            "teamASeed": a_seed, "teamBSeed": b_seed,
            "teamAId": None, "teamBId": None,
            "status": status,
            "winnerId": None, "sets": [],
        }

    def _check_results_playoff_logic(self, data, entry):
        """
        Extract just the playoff guard logic from check_results to test it
        without async/network overhead.
        """
        our_seed    = entry.get("team_seed")
        team_id     = entry.get("team_id")
        bracket_tid = entry.get("bracket_team_id")

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

        if not in_bracket:
            return False

        player_matches = [
            m for m in data
            if (team_id     and (m.get("teamAId") == team_id     or m.get("teamBId") == team_id))
            or (bracket_tid and (m.get("teamAId") == bracket_tid or m.get("teamBId") == bracket_tid))
            or (our_seed    and (m.get("teamASeed") == our_seed  or m.get("teamBSeed") == our_seed))
        ]
        return any(
            m.get("status") not in ("scheduled", "not_started", None)
            for m in player_matches
        )

    def test_no_email_when_all_scheduled(self):
        # Simulates bracket published at tournament start — all matches "scheduled"
        data = [
            self._scheduled_match(9, 8),   # Selina's R0 match (seed 9 vs 8)
            self._scheduled_match(5, 12),
            self._scheduled_match(1, None),
        ]
        entry = {"team_seed": 9}
        self.assertFalse(self._check_results_playoff_logic(data, entry))

    def test_email_fires_when_match_in_progress(self):
        data = [
            self._active_match(9, 8, "in_progress"),
            self._scheduled_match(5, 12),
        ]
        entry = {"team_seed": 9}
        self.assertTrue(self._check_results_playoff_logic(data, entry))

    def test_email_fires_when_match_completed(self):
        data = [
            self._active_match(9, 8, "completed"),
        ]
        entry = {"team_seed": 9}
        self.assertTrue(self._check_results_playoff_logic(data, entry))

    def test_no_email_when_player_not_in_bracket(self):
        # Player seed 3 not in this bracket
        data = [self._scheduled_match(9, 8)]
        entry = {"team_seed": 3}
        self.assertFalse(self._check_results_playoff_logic(data, entry))

    def test_source_has_match_started_guard(self):
        import inspect
        src = inspect.getsource(monitor.check_results)
        self.assertIn(
            "match_started", src,
            "check_results must have a match_started guard to prevent "
            "false playoff emails on bracket initialisation",
        )


# ── Regression: getPools nested structure ─────────────────────────────────────

class TestNormalizeMatchList(unittest.TestCase):
    """
    getPools returns [{..., "matches": [...]}, ...] — a list of pool objects
    containing match sub-lists.  _normalize_match_list must flatten this to
    a plain match list so the rest of the code can iterate over matches directly.
    """

    def _pool_match(self, status):
        return {
            "teamAId": 100, "teamBId": 200,
            "teamASeed": 1, "teamBSeed": 2,
            "status": status, "winnerId": None, "sets": [],
        }

    def test_flat_list_unchanged(self):
        matches = [self._pool_match("scheduled"), self._pool_match("completed")]
        result = monitor._normalize_match_list(matches)
        self.assertEqual(result, matches)

    def test_nested_pool_structure_flattened(self):
        # getPools response: list of pool objects, each with a "matches" key
        pool_data = [
            {"id": 1, "name": "Pool A", "done": False, "matches": [
                self._pool_match("completed"),
                self._pool_match("scheduled"),
            ]},
            {"id": 2, "name": "Pool B", "done": False, "matches": [
                self._pool_match("in_progress"),
            ]},
        ]
        result = monitor._normalize_match_list(pool_data)
        self.assertEqual(len(result), 3)
        statuses = {m["status"] for m in result}
        self.assertEqual(statuses, {"completed", "scheduled", "in_progress"})

    def test_completed_match_detected_after_normalization(self):
        pool_data = [
            {"id": 1, "done": True, "matches": [self._pool_match("completed")]},
        ]
        flat = monitor._normalize_match_list(pool_data)
        has_active = any(
            m.get("status") not in ("scheduled", "not_started", None)
            for m in flat
        )
        self.assertTrue(has_active, "completed pool match must be seen as active after normalization")

    def test_empty_list_returns_empty(self):
        self.assertEqual(monitor._normalize_match_list([]), [])

    def test_non_list_returns_empty(self):
        self.assertEqual(monitor._normalize_match_list(None), [])
        self.assertEqual(monitor._normalize_match_list({"foo": "bar"}), [])


# ── Tournament Availability Tracker ──────────────────────────────────────────

# ── _t_date_str ───────────────────────────────────────────────────────────────

class TestTDateStr(unittest.TestCase):
    def test_iso_date_string(self):
        result = monitor._t_date_str({"date": "2026-07-04T00:00:00Z"})
        self.assertIn("2026", result)
        self.assertIn("July", result)

    def test_fallback_field_order(self):
        # startDate should be used when date is absent
        result = monitor._t_date_str({"startDate": "2026-07-04T00:00:00Z"})
        self.assertIn("2026", result)

    def test_empty_returns_empty_string(self):
        self.assertEqual(monitor._t_date_str({}), "")

    def test_non_iso_returns_slice(self):
        result = monitor._t_date_str({"date": "2026-08-15 raw"})
        self.assertEqual(result, "2026-08-15")   # first 10 chars


# ── Tournament Availability Tracker ──────────────────────────────────────────

class TestDivGenderLabel(unittest.TestCase):
    def _div(self, gender, max_age=None):
        return {"gender": gender, "division": {"maxAge": max_age}}

    def test_male_adult(self):
        self.assertEqual(monitor._div_gender_label(self._div("male")), "Men's")

    def test_female_adult(self):
        self.assertEqual(monitor._div_gender_label(self._div("female")), "Women's")

    def test_coed(self):
        self.assertEqual(monitor._div_gender_label(self._div("coed")), "Coed")

    def test_male_youth(self):
        self.assertEqual(monitor._div_gender_label(self._div("male", max_age=16)), "Boys")

    def test_female_youth(self):
        self.assertEqual(monitor._div_gender_label(self._div("female", max_age=16)), "Girls")


class TestDivLevelLabel(unittest.TestCase):
    def _div(self, display):
        return {"division": {"display": display}}

    def test_a(self):
        self.assertEqual(monitor._div_level_label(self._div("A")), "A")

    def test_aa_lowercase(self):
        self.assertEqual(monitor._div_level_label(self._div("aa")), "AA")

    def test_open(self):
        self.assertEqual(monitor._div_level_label(self._div("Open")), "OPEN")

    def test_empty_is_u(self):
        self.assertEqual(monitor._div_level_label(self._div("")), "U")


class TestNormDivLevel(unittest.TestCase):
    def test_b(self):
        self.assertEqual(monitor._norm_div_level("B"), "B")

    def test_unrated_variants(self):
        for v in ("unrated", "n", "u", "N", "U", ""):
            self.assertEqual(monitor._norm_div_level(v), "U", f"Failed for {v!r}")

    def test_open_variants(self):
        self.assertEqual(monitor._norm_div_level("open"), "OPEN")
        self.assertEqual(monitor._norm_div_level("OPEN"), "OPEN")


class TestDivMatchesWatch(unittest.TestCase):
    def _div(self, gender="male", level="A"):
        return {"gender": gender, "division": {"display": level}}

    def test_no_filters_matches_all(self):
        self.assertTrue(monitor._div_matches_watch(self._div("male", "B"), {}))
        self.assertTrue(monitor._div_matches_watch(self._div("female", "AAA"), {}))

    def test_gender_filter_matches(self):
        w = {"genders": ["Men's"]}
        self.assertTrue(monitor._div_matches_watch(self._div("male"), w))
        self.assertFalse(monitor._div_matches_watch(self._div("female"), w))

    def test_division_filter_matches(self):
        w = {"divisions": ["A", "AA"]}
        self.assertTrue(monitor._div_matches_watch(self._div(level="A"), w))
        self.assertTrue(monitor._div_matches_watch(self._div(level="AA"), w))
        self.assertFalse(monitor._div_matches_watch(self._div(level="B"), w))

    def test_both_filters_must_match(self):
        w = {"genders": ["Men's"], "divisions": ["A"]}
        self.assertTrue(monitor._div_matches_watch(self._div("male", "A"), w))
        self.assertFalse(monitor._div_matches_watch(self._div("male", "B"), w))
        self.assertFalse(monitor._div_matches_watch(self._div("female", "A"), w))

    def test_coed_filter(self):
        w = {"genders": ["Coed"]}
        self.assertTrue(monitor._div_matches_watch({"gender": "coed", "division": {}}, w))
        self.assertFalse(monitor._div_matches_watch(self._div("male"), w))

    def test_open_div_matches(self):
        w = {"divisions": ["OPEN"]}
        self.assertTrue(monitor._div_matches_watch(self._div(level="Open"), w))
        self.assertTrue(monitor._div_matches_watch(self._div(level="OPEN"), w))


class TestParseRegStatus(unittest.TestCase):
    def test_register_open(self):
        # Actual CBVA button: "Register — $XX.XX" (price may vary)
        self.assertEqual(monitor._parse_reg_status("Register — $80.00"), "open")
        self.assertEqual(monitor._parse_reg_status("Register"), "open")

    def test_sign_up_now_footer_not_open(self):
        # "SIGN UP NOW" appears in the footer of every CBVA page — must NOT be treated as open
        self.assertEqual(monitor._parse_reg_status("WAITLIST FULL\nSIGN UP NOW"), "waitlist_full")

    def test_sign_up_now_footer_alone_not_open(self):
        # Footer alone with no registration button = unknown
        self.assertEqual(monitor._parse_reg_status("Tournament info\nSIGN UP NOW"), "unknown")

    def test_join_waitlist(self):
        # Actual CBVA button: "Join Waitlist — $80.00"
        self.assertEqual(monitor._parse_reg_status("Tournament info\nJoin Waitlist — $80.00"), "waitlist")

    def test_waitlist_full_before_register(self):
        # "WAITLIST FULL" must take priority even if page contains "Register" text
        self.assertEqual(monitor._parse_reg_status("WAITLIST FULL — registration closed"), "waitlist_full")

    def test_coming_soon(self):
        self.assertEqual(monitor._parse_reg_status("Coming soon to a beach near you"), "coming_soon")

    def test_registration_closed(self):
        self.assertEqual(monitor._parse_reg_status("REGISTRATION CLOSED"), "closed")

    def test_unknown(self):
        self.assertEqual(monitor._parse_reg_status("Tournament page"), "unknown")


class TestRegStatusRank(unittest.TestCase):
    def test_open_beats_all(self):
        for s in ("unknown", "coming_soon", "closed", "waitlist_full", "waitlist"):
            self.assertGreater(
                monitor._REG_RANK["open"],
                monitor._REG_RANK[s],
                f"open should beat {s}",
            )

    def test_waitlist_beats_waitlist_full(self):
        self.assertGreater(monitor._REG_RANK["waitlist"], monitor._REG_RANK["waitlist_full"])

    def test_waitlist_full_beats_closed(self):
        self.assertGreater(monitor._REG_RANK["waitlist_full"], monitor._REG_RANK["closed"])


class TestParseIso(unittest.TestCase):
    def test_z_suffix(self):
        dt = monitor._parse_iso("2026-05-29T01:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_naive_treated_as_utc(self):
        dt = monitor._parse_iso("2026-05-29T01:00:00")
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def test_none_and_garbage(self):
        self.assertIsNone(monitor._parse_iso(None))
        self.assertIsNone(monitor._parse_iso(""))
        self.assertIsNone(monitor._parse_iso("not-a-date"))


class TestRegStatusFromDivision(unittest.TestCase):
    """API-based status detection (capacity numbers), validated against live data."""

    def _div(self, cap=10, conf=2, wcap=5, wl=0, status="closed", paused=False):
        return {"capacity": cap, "confirmedCount": conf, "waitlistCapacity": wcap,
                "waitlistedCount": wl, "status": status, "registrationPaused": paused}

    def test_open_when_main_has_space(self):
        # live: cap 10 conf 2/6 -> Register
        self.assertEqual(monitor._reg_status_from_division(self._div(conf=2), None, None), "open")
        self.assertEqual(monitor._reg_status_from_division(self._div(conf=6), None, None), "open")

    def test_waitlist_when_main_full_waitlist_open(self):
        # live: cap 30 conf 30, wcap 7 wl 6 -> Join Waitlist
        d = self._div(cap=30, conf=30, wcap=7, wl=6)
        self.assertEqual(monitor._reg_status_from_division(d, None, None), "waitlist")

    def test_waitlist_full_when_both_full(self):
        # live: cap 20 conf 20, wcap 5 wl 5 -> Waitlist Full
        d = self._div(cap=20, conf=20, wcap=5, wl=5)
        self.assertEqual(monitor._reg_status_from_division(d, None, None), "waitlist_full")

    def test_oversubscribed_main_counts_as_full(self):
        d = self._div(cap=20, conf=21, wcap=5, wl=2)
        self.assertEqual(monitor._reg_status_from_division(d, None, None), "waitlist")

    def test_paused_is_closed(self):
        # live: running tournaments have registrationPaused True
        d = self._div(cap=15, conf=14, paused=True)
        self.assertEqual(monitor._reg_status_from_division(d, None, None), "closed")

    def test_running_status_is_closed(self):
        d = self._div(cap=15, conf=14, status="running")
        self.assertEqual(monitor._reg_status_from_division(d, None, None), "closed")

    def test_coming_soon_when_registration_not_open(self):
        now  = datetime(2026, 6, 1, tzinfo=timezone.utc)
        open_at = datetime(2026, 6, 15, tzinfo=timezone.utc)
        d = self._div(conf=0)
        self.assertEqual(monitor._reg_status_from_division(d, open_at, now), "coming_soon")

    def test_open_when_registration_already_opened(self):
        now  = datetime(2026, 6, 20, tzinfo=timezone.utc)
        open_at = datetime(2026, 6, 15, tzinfo=timezone.utc)
        d = self._div(conf=0)
        self.assertEqual(monitor._reg_status_from_division(d, open_at, now), "open")

    def test_none_when_capacity_data_missing(self):
        # Signals the caller to fall back to text parsing
        self.assertIsNone(monitor._reg_status_from_division({"capacity": None}, None, None))
        self.assertIsNone(monitor._reg_status_from_division({"confirmedCount": 5}, None, None))

    def test_main_full_no_waitlist_info_is_waitlist_full(self):
        d = {"capacity": 10, "confirmedCount": 10}
        self.assertEqual(monitor._reg_status_from_division(d, None, None), "waitlist_full")


# ── Email builders ─────────────────────────────────────────────────────────────

_PT = timezone(timedelta(hours=-7))


def _now():
    return datetime.now(_PT)


def _div(gender="male", level="A", div_id=500):
    return {"id": div_id, "gender": gender, "division": {"display": level}}


def _record(city="San Diego", t_name="SD Open", divs=None):
    if divs is None:
        divs = [_div()]
    return {
        "t_id": 100, "t_name": t_name,
        "venue_str": "Mission Bay Park, San Diego",
        "t_date": "July 4, 2026",
        "t_url": "https://cbva.com/tournaments/100",
        "watch": {"city": city, "genders": ["Men's"], "divisions": ["A"]},
        "new_divs": divs,
    }


class TestBuildNewTournamentEmail(unittest.TestCase):
    def test_returns_valid_html(self):
        html = monitor._build_new_tournament_email([_record()], _now())
        self.assertIn("<html>", html)
        self.assertIn("</html>", html)

    def test_contains_tournament_name(self):
        html = monitor._build_new_tournament_email([_record(t_name="Beach Bash 2026")], _now())
        self.assertIn("Beach Bash 2026", html)

    def test_contains_venue_and_date(self):
        html = monitor._build_new_tournament_email([_record()], _now())
        self.assertIn("Mission Bay Park", html)
        self.assertIn("July 4, 2026", html)

    def test_contains_city_watch_label(self):
        html = monitor._build_new_tournament_email([_record(city="San Diego")], _now())
        self.assertIn("San Diego", html)
        self.assertIn("Men's", html)

    def test_division_link_present(self):
        html = monitor._build_new_tournament_email([_record()], _now())
        self.assertIn("/tournaments/100/500", html)  # div id 500 in URL

    def test_multiple_records_all_shown(self):
        records = [_record("San Diego", "Open A"), _record("San Diego", "Classic B")]
        html = monitor._build_new_tournament_email(records, _now())
        self.assertIn("Open A", html)
        self.assertIn("Classic B", html)

    def test_no_filter_label_when_no_filters(self):
        r = _record()
        r["watch"] = {"city": "San Diego", "genders": [], "divisions": []}
        html = monitor._build_new_tournament_email([r], _now())
        self.assertIn("San Diego", html)

    def test_multiple_divisions_all_linked(self):
        divs = [_div("male", "A", 500), _div("female", "A", 501)]
        html = monitor._build_new_tournament_email([_record(divs=divs)], _now())
        self.assertIn("/tournaments/100/500", html)
        self.assertIn("/tournaments/100/501", html)


class TestRegisterDeeplink(unittest.TestCase):
    def test_matches_cbva_format(self):
        # Byte-for-byte the same as the on-page Register/Join Waitlist href
        self.assertEqual(
            monitor._register_deeplink(16482),
            "https://cbva.com/account/registrations?teams="
            "%5B%7B%22divisionId%22%3A16482%2C%22profileIds%22%3A%5B%5D%7D%5D",
        )

    def test_division_id_embedded(self):
        self.assertIn("%22divisionId%22%3A500", monitor._register_deeplink(500))


class TestBuildUrlStatusEmail(unittest.TestCase):
    def _call(self, old="waitlist_full", new="waitlist"):
        return monitor._build_url_status_email(
            "https://cbva.com/tournaments/100/500",
            old, new,
            "SD Open", "Men's A",
            "Mission Bay Park, San Diego", "July 4, 2026",
            _now(),
        )

    def test_open_email_has_register_deeplink(self):
        html = self._call("waitlist", "open")
        self.assertIn("/account/registrations?teams=", html)
        self.assertIn("%22divisionId%22%3A500", html)  # division pre-selected

    def test_waitlist_email_has_register_deeplink(self):
        html = self._call("waitlist_full", "waitlist")
        self.assertIn("/account/registrations?teams=", html)
        self.assertIn("%22divisionId%22%3A500", html)

    def test_tournament_page_kept_as_secondary_link(self):
        html = self._call("waitlist_full", "waitlist")
        self.assertIn("View tournament details", html)
        self.assertIn("https://cbva.com/tournaments/100/500", html)

    def test_non_registerable_status_falls_back_to_tournament_page(self):
        # coming_soon: no register deep-link, CTA points at the tournament page
        html = self._call("unknown", "coming_soon")
        self.assertNotIn("/account/registrations", html)
        self.assertIn("View Tournament", html)

    def test_returns_valid_html(self):
        html = self._call()
        self.assertIn("<html>", html)
        self.assertIn("</html>", html)

    def test_shows_old_status_crossed_out(self):
        html = self._call("waitlist_full", "waitlist")
        self.assertIn("Waitlist Full", html)
        self.assertIn("line-through", html)

    def test_shows_new_status_highlighted(self):
        html = self._call("waitlist_full", "waitlist")
        self.assertIn("Join Waitlist", html)

    def test_open_status_green_and_register_button(self):
        html = self._call("waitlist", "open")
        self.assertIn("Register", html)
        self.assertIn("#1D9E75", html)  # green

    def test_waitlist_status_purple_button(self):
        html = self._call("waitlist_full", "waitlist")
        self.assertIn("#6A5ACD", html)  # purple

    def test_contains_tournament_name(self):
        html = self._call()
        self.assertIn("SD Open", html)
        self.assertIn("Men's A", html)

    def test_url_in_button(self):
        html = self._call()
        self.assertIn("https://cbva.com/tournaments/100/500", html)

    def test_venue_and_date_shown(self):
        html = self._call()
        self.assertIn("Mission Bay Park", html)
        self.assertIn("July 4, 2026", html)


# ── Async: check_url_statuses ─────────────────────────────────────────────────

class TestCheckUrlStatusesAsync(unittest.IsolatedAsyncioTestCase):

    def _page(self, body_text="Register"):
        p = MagicMock()
        p.url = "https://cbva.com/tournaments"   # already on-origin → no setup nav
        p.goto = AsyncMock()
        p.evaluate = AsyncMock(return_value=body_text)
        return p

    def _state(self, url, last_status):
        return {"tournament_tracker": {"url_watches": {url: {"last_status": last_status}}}}

    URL = "https://cbva.com/tournaments/100/500"

    async def test_first_check_no_email(self):
        """First time URL is seen: records status silently, no email."""
        page = self._page("WAITLIST FULL")
        state = {}
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_not_called()
        saved = state["tournament_tracker"]["url_watches"][self.URL]["last_status"]
        self.assertEqual(saved, "waitlist_full")

    async def test_improvement_sends_email(self):
        """Status improves waitlist_full → waitlist: email sent."""
        page = self._page("JOIN WAITLIST")
        state = self._state(self.URL, "waitlist_full")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]
        details = {
            "name": "SD Open", "venue": {"name": "Mission Bay", "city": "San Diego"},
            "tournamentDivisions": [{"id": 500, "gender": "male", "division": {"display": "A"}}],
        }

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_called_once()
        subject = mock_email.call_args[0][0]
        self.assertIn("SD Open", subject)

    def _details(self, div_id=500, cap=30, conf=30, wcap=5, wl=2,
                 paused=False, status="closed", reg_open=None):
        return {
            "name": "SD Open", "venue": {"name": "Mission Bay", "city": "San Diego"},
            "registrationOpenAt": reg_open,
            "tournamentDivisions": [{
                "id": div_id, "gender": "male", "division": {"display": "A"},
                "capacity": cap, "confirmedCount": conf,
                "waitlistCapacity": wcap, "waitlistedCount": wl,
                "status": status, "registrationPaused": paused,
            }],
        }

    async def test_api_path_improvement_without_navigation(self):
        """waitlist_full → waitlist from capacity numbers; no page.goto needed."""
        page = self._page()
        state = self._state(self.URL, "waitlist_full")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]
        details = self._details(cap=30, conf=30, wcap=7, wl=6)  # waitlist

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_called_once()
        page.goto.assert_not_called()  # API path → no scraping
        self.assertEqual(state["tournament_tracker"]["url_watches"][self.URL]["last_status"], "waitlist")

    async def test_api_path_unchanged_no_email_no_navigation(self):
        page = self._page()
        state = self._state(self.URL, "open")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]
        details = self._details(cap=10, conf=2)  # open

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_not_called()
        page.goto.assert_not_called()

    async def test_falls_back_to_text_when_no_capacity_data(self):
        """Division present but without capacity numbers → text scrape fallback."""
        page = self._page("JOIN WAITLIST")
        state = self._state(self.URL, "waitlist_full")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]
        details = {"name": "SD Open", "venue": {},
                   "tournamentDivisions": [{"id": 500, "gender": "male", "division": {"display": "A"}}]}

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        page.goto.assert_called_once()       # fell back to scraping
        mock_email.assert_called_once()       # waitlist_full → waitlist

    async def test_prunes_unwatched_urls_from_state(self):
        """url_watches entries no one is tracking anymore are dropped (bounds growth)."""
        page = self._page()
        stale_url = "https://cbva.com/tournaments/999/888"
        state = {"tournament_tracker": {"url_watches": {
            self.URL:  {"last_status": "open"},
            stale_url: {"last_status": "waitlist"},
        }}}
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]
        details = self._details(cap=10, conf=2)  # open, unchanged

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
            with patch.object(monitor, "send_email"):
                await monitor.check_url_statuses(page, state, users, _now())

        ws = state["tournament_tracker"]["url_watches"]
        self.assertIn(self.URL, ws)
        self.assertNotIn(stale_url, ws)

    async def test_open_improvement_sends_email(self):
        """waitlist → open (best status) also sends email."""
        page = self._page("Register")
        state = self._state(self.URL, "waitlist")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_called_once()

    async def test_no_change_no_email(self):
        """Same status on re-check: no email."""
        page = self._page("WAITLIST FULL")
        state = self._state(self.URL, "waitlist_full")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_not_called()

    async def test_status_worsens_no_email(self):
        """Status worsening (open → waitlist_full): no email."""
        page = self._page("WAITLIST FULL")
        state = self._state(self.URL, "open")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_not_called()

    async def test_state_updated_after_check(self):
        """last_status in state reflects what was found on page."""
        page = self._page("JOIN WAITLIST")
        state = self._state(self.URL, "waitlist_full")
        users = [{"email": "a@b.com", "tournament_urls": [self.URL]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email", new=MagicMock()):
                await monitor.check_url_statuses(page, state, users, _now())

        saved = state["tournament_tracker"]["url_watches"][self.URL]["last_status"]
        self.assertEqual(saved, "waitlist")

    async def test_no_urls_skips_network(self):
        """Users with no tournament_urls: no page.goto called."""
        page = self._page()
        state = {}
        users = [{"email": "a@b.com", "players": ["Alice"]}]

        await monitor.check_url_statuses(page, state, users, _now())

        page.goto.assert_not_called()

    async def test_only_watching_user_notified(self):
        """Only the user who listed the URL gets the email, not all users."""
        page = self._page("Register")
        state = self._state(self.URL, "waitlist")
        users = [
            {"email": "watcher@b.com", "tournament_urls": [self.URL]},
            {"email": "other@b.com",   "tournament_urls": []},
        ]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        self.assertEqual(mock_email.call_count, 1)
        self.assertIn("watcher@b.com", mock_email.call_args[1].get("to", ""))

    async def test_bad_url_skipped(self):
        """Unrecognised URL format: skip gracefully without crashing."""
        page = self._page()
        state = {}
        users = [{"email": "a@b.com", "tournament_urls": ["https://notcbva.com/foo"]}]

        await monitor.check_url_statuses(page, state, users, _now())

        page.goto.assert_not_called()

    async def test_dict_entry_improvement_sends_email(self):
        """Dict-format entry {url, nickname} works the same as a plain string."""
        page = self._page("Register")
        state = self._state(self.URL, "waitlist")
        users = [{"email": "a@b.com", "tournament_urls": [{"url": self.URL, "nickname": "SD Open Men's A"}]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        mock_email.assert_called_once()

    async def test_mixed_string_and_dict_entries(self):
        """Mixed list (some strings, some dicts) all resolve correctly."""
        url2 = "https://cbva.com/tournaments/200/501"
        page = MagicMock()
        page.goto = AsyncMock()
        page.evaluate = AsyncMock(side_effect=["Register", "JOIN WAITLIST"])
        state = self._state(self.URL, "waitlist")
        state["tournament_tracker"]["url_watches"][url2] = {"last_status": "waitlist_full"}
        users = [{"email": "a@b.com", "tournament_urls": [
            self.URL,
            {"url": url2, "nickname": "Other tourney"},
        ]}]

        with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=None)):
            with patch.object(monitor, "send_email") as mock_email:
                await monitor.check_url_statuses(page, state, users, _now())

        self.assertEqual(mock_email.call_count, 2)


# ── Async: check_city_tournaments ─────────────────────────────────────────────

class TestCheckCityTournamentsAsync(unittest.IsolatedAsyncioTestCase):

    def _page(self):
        p = MagicMock()
        p.goto = AsyncMock()
        return p

    def _upcoming(self):
        return [{"id": 100, "name": "SD Open", "venue": {"city": "San Diego", "name": "Mission Bay"}}]

    def _details(self, div_list=None):
        if div_list is None:
            div_list = [
                {"id": 500, "gender": "male",   "division": {"display": "A"}},
                {"id": 501, "gender": "female", "division": {"display": "A"}},
            ]
        return {
            "name": "SD Open",
            "venue": {"city": "San Diego", "name": "Mission Bay"},
            "tournamentDivisions": div_list,
        }

    def _user(self, watches=None, email="a@b.com"):
        return {
            "email": email,
            "tournament_watches": watches or [{"city": "San Diego", "genders": [], "divisions": []}],
            "players": [],
        }

    async def test_new_tournament_sends_email(self):
        """New tournament in watched city → email sent, both divisions recorded."""
        page = self._page()
        state = {}
        users = [self._user()]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_called_once()
        ws = state["tournament_tracker"]["city_watches"]["a@b.com:san diego"]
        self.assertIn("100:500", ws["seen_tdivs"])
        self.assertIn("100:501", ws["seen_tdivs"])

    async def test_gender_filter_only_matching_div_recorded(self):
        """Men's filter: only Men's division in seen_tdivs, Women's excluded."""
        page = self._page()
        state = {}
        users = [self._user(watches=[{"city": "San Diego", "genders": ["Men's"], "divisions": []}])]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_called_once()
        ws = state["tournament_tracker"]["city_watches"]["a@b.com:san diego"]
        self.assertIn("100:500",    ws["seen_tdivs"])   # Men's A matched
        self.assertNotIn("100:501", ws["seen_tdivs"])   # Women's A filtered out

    async def test_division_filter_applied(self):
        """Division filter B: A-level division excluded, B-level included."""
        page = self._page()
        state = {}
        users = [self._user(watches=[{"city": "San Diego", "genders": [], "divisions": ["B"]}])]
        details = self._details(div_list=[
            {"id": 500, "gender": "male", "division": {"display": "A"}},
            {"id": 502, "gender": "male", "division": {"display": "B"}},
        ])

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_called_once()
        ws = state["tournament_tracker"]["city_watches"]["a@b.com:san diego"]
        self.assertNotIn("100:500", ws["seen_tdivs"])   # Men's A filtered
        self.assertIn("100:502",    ws["seen_tdivs"])   # Men's B matched

    async def test_already_seen_no_email(self):
        """All divisions already in seen_tdivs → no email, no duplicate."""
        page = self._page()
        state = {
            "tournament_tracker": {
                "city_watches": {
                    "a@b.com:san diego": {"seen_tdivs": ["100:500", "100:501"]}
                }
            }
        }
        users = [self._user()]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_not_called()

    async def test_partially_seen_emails_only_new(self):
        """One division already seen, one new → email fires for the new one only."""
        page = self._page()
        state = {
            "tournament_tracker": {
                "city_watches": {
                    "a@b.com:san diego": {"seen_tdivs": ["100:500"]}  # Men's A already seen
                }
            }
        }
        users = [self._user()]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_called_once()
        ws = state["tournament_tracker"]["city_watches"]["a@b.com:san diego"]
        self.assertIn("100:501", ws["seen_tdivs"])   # Women's A now added

    async def test_already_checked_today_skips_fetch(self):
        """city_last_checked = today → no network call."""
        page = self._page()
        today = date.today().strftime("%Y-%m-%d")
        state = {"tournament_tracker": {"city_last_checked": today}}
        users = [self._user()]

        mock_fetch = AsyncMock()
        with patch.object(monitor, "_fetch_upcoming_tournaments", new=mock_fetch):
            await monitor.check_city_tournaments(page, state, users, _now())

        mock_fetch.assert_not_called()

    async def test_wrong_city_no_email(self):
        """Tournament in San Diego, user watches Los Angeles → no email."""
        page = self._page()
        state = {}
        users = [self._user(watches=[{"city": "Los Angeles", "genders": [], "divisions": []}])]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_not_called()

    async def test_city_match_is_case_insensitive(self):
        """'SAN DIEGO' in config matches 'San Diego' in API venue.city."""
        page = self._page()
        state = {}
        users = [self._user(watches=[{"city": "SAN DIEGO", "genders": [], "divisions": []}])]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_called_once()

    async def test_no_watches_skips_all_network(self):
        """User has no tournament_watches → _fetch_upcoming_tournaments never called."""
        page = self._page()
        state = {}
        users = [{"email": "a@b.com", "players": ["Alice"]}]

        mock_fetch = AsyncMock()
        with patch.object(monitor, "_fetch_upcoming_tournaments", new=mock_fetch):
            await monitor.check_city_tournaments(page, state, users, _now())

        mock_fetch.assert_not_called()

    async def test_per_user_state_independent(self):
        """Two users watching same city get independent seen_tdivs state."""
        page = self._page()
        state = {
            "tournament_tracker": {
                "city_watches": {
                    "user1@b.com:san diego": {"seen_tdivs": ["100:500", "100:501"]},
                    # user2 hasn't seen anything yet
                }
            }
        }
        users = [
            self._user(email="user1@b.com"),
            self._user(email="user2@b.com"),
        ]

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=self._details())):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        # user1 already saw both → no email; user2 is new → email
        self.assertEqual(mock_email.call_count, 1)
        self.assertIn("user2@b.com", mock_email.call_args[1].get("to", ""))

    async def test_no_divisions_match_no_email(self):
        """All divisions filtered out → no email even though tournament is in city."""
        page = self._page()
        state = {}
        users = [self._user(watches=[{"city": "San Diego", "genders": ["Men's"], "divisions": ["AAA"]}])]
        # Only A-level divisions in the tournament
        details = self._details(div_list=[
            {"id": 500, "gender": "male", "division": {"display": "A"}},
        ])

        with patch.object(monitor, "_fetch_upcoming_tournaments", new=AsyncMock(return_value=self._upcoming())):
            with patch.object(monitor, "_trpc_get", new=AsyncMock(return_value=details)):
                with patch.object(monitor, "send_email") as mock_email:
                    await monitor.check_city_tournaments(page, state, users, _now())

        mock_email.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
