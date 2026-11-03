from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.calendar_feed import (
    Game, TEAMS, get_live_games, parse_espn_schedule, parse_osu_schedule,
    reconcile_osu_games, write_feeds, EASTERN, SUPERSEDED_UIDS,
)

ROOT = Path(__file__).resolve().parents[1]
OSU_HTML = (ROOT / "tests/fixtures/osu-2026.html").read_text()
NOW = datetime(2026, 11, 3, 18, tzinfo=UTC)


def schedule(day="Sep 12 (Sat)", opponent="Texas", season=2026):
    return (f"<h1>{season} Football Schedule</h1><table><tr><td>{day}</td>"
            f"<td>7:30 PM</td><td>Away</td><td>{opponent}</td><td>Austin, Texas</td>"
            "</tr></table>")


def events(output: Path, slug: str) -> list[str]:
    text = (output / f"{slug}.ics").read_text().replace("\n ", "")
    return [part.split("END:VEVENT")[0] for part in text.split("BEGIN:VEVENT")[1:]]


def nfl_event(event_id="shared", week=13):
    return {
        "id": event_id, "date": "2026-12-06T21:00Z", "timeValid": True,
        "season": {"year": 2026, "type": 2}, "week": {"number": week},
        "competitions": [{"timeValid": True, "competitors": [
            {"homeAway": "away", "team": {"id": "25", "displayName": TEAMS[1].name}},
            {"homeAway": "home", "team": {"id": "17", "displayName": TEAMS[2].name}},
        ]}],
    }


class FixtureIsolationTests(unittest.TestCase):
    def invoke(self, cwd, *args):
        return subprocess.run([
            sys.executable, "-B", str(ROOT / "scripts/calendar_feed.py"),
            "--fixture", str(ROOT / "tests/fixtures/sample.json"), "--output", "output", *args,
        ], cwd=cwd, capture_output=True, text=True)

    def test_fixture_default_state_is_isolated_and_provenance_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            live = root / "data/feed-state.json"
            live.write_text('{"sentinel": true}\n')
            result = self.invoke(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(live.read_text(), '{"sentinel": true}\n')
            self.assertTrue((root / "output/feed-state.json").is_file())
            health = json.loads((root / "output/health.json").read_text())
            self.assertEqual(health["source"], "fixture")
            self.assertEqual(health["events"], 3)

    def test_fixture_explicit_state_path_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.invoke(root, "--state", "isolated/state.json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "isolated/state.json").is_file())
            self.assertFalse((root / "data/feed-state.json").exists())
            self.assertFalse((root / "output/feed-state.json").exists())

    def test_fixture_rejects_live_state_even_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            live = root / "data/feed-state.json"
            live.write_text('{}\n')
            (root / "alias.json").symlink_to(live)
            for state in ("data/feed-state.json", "alias.json", str(ROOT / "data/feed-state.json")):
                with self.subTest(state=state):
                    result = self.invoke(root, "--state", state)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("fixture state must be separate", result.stderr)
                    self.assertFalse((root / "output").exists())
            self.assertEqual(live.read_text(), '{}\n')


class SourceSeasonTests(unittest.TestCase):
    def test_source_heading_is_required_and_must_be_unambiguous(self):
        html = schedule()
        for invalid in (html.replace("<h1>2026 Football Schedule</h1>", ""),
                        html + "<h2>2027 Football Schedule</h2>"):
            with self.subTest(html=invalid), self.assertRaisesRegex(ValueError, "source season"):
                parse_osu_schedule(invalid)

    def test_postseason_uses_next_calendar_year_but_original_season(self):
        for day, expected in (("Dec 31 (Thu)", date(2026, 12, 31)),
                              ("Jan 1 (Fri)", date(2027, 1, 1)),
                              ("Feb 1 (Mon)", date(2027, 2, 1))):
            with self.subTest(day=day):
                game = parse_osu_schedule(schedule(day))[0]
                self.assertEqual(game.season, 2026)
                self.assertEqual(game.game_date, expected)
                self.assertEqual(game.start.astimezone(EASTERN).date(), expected)
                if expected.year == 2027:
                    self.assertEqual(game.season_type, "Postseason")

    def test_stale_fall_schedule_does_not_move_forward_on_january_refresh(self):
        printable = ("<table><tr><td>September 12, 2026</td><td>7:30 PM</td><td>Away</td>"
                     "<td>Texas</td><td>Austin, Texas</td><td>ABC</td></tr></table>")

        def fetch_json(url):
            if "/schedule?" in url and "seasontype=2" in url:
                return {"events": [nfl_event(str(week), week) for week in range(1, 18)]}
            return {}

        with patch("scripts.calendar_feed.fetch_text", side_effect=lambda url: printable if url.endswith("/print") else OSU_HTML), \
                patch("scripts.calendar_feed.fetch_json", side_effect=fetch_json):
            december = get_live_games(date(2026, 12, 31))
            january = get_live_games(date(2027, 1, 1))
        before = [(g.uid, g.game_date, g.season) for g in december if g.league == "college-football"]
        after = [(g.uid, g.game_date, g.season) for g in january if g.league == "college-football"]
        self.assertEqual(len(before), 12)
        self.assertEqual(before, after)
        self.assertTrue(all(day.year == season == 2026 for _, day, season in after))

    def test_leap_day_is_validated_in_the_postseason_calendar_year(self):
        game = parse_osu_schedule(schedule("Feb 29 (Tue)", season=2027))[0]
        self.assertEqual(game.game_date, date(2028, 2, 29))


class OhioStateIdentityTests(unittest.TestCase):
    def test_published_uid_set_is_preserved_when_registering_identity(self):
        state = json.loads((ROOT / "tests/fixtures/osu-published-state.json").read_text())
        games = parse_osu_schedule(OSU_HTML)
        published_uids = set(state) - set(SUPERSEDED_UIDS)
        before = copy.deepcopy(state)
        resolved = reconcile_osu_games(games, state)
        self.assertEqual({game.uid for game in resolved}, published_uids)
        self.assertEqual({uid: {key: value for key, value in entry.items() if key != "schedule_id"}
                          for uid, entry in state.items()}, before)

    def test_rescheduled_published_game_keeps_uid_sequence_and_cancellation(self):
        state = json.loads((ROOT / "tests/fixtures/osu-published-state.json").read_text())
        original = parse_osu_schedule(schedule())[0]
        old = copy.deepcopy(state[original.uid])
        moved = parse_osu_schedule(schedule("Sep 13 (Sun)", "#4 TEXAS"))[0]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "state.json"
            path.write_text(json.dumps(state))
            write_feeds([moved], output, path, NOW)
            first_state = path.read_bytes()
            for slug in ("ohio-state", "football"):
                active, cancelled = events(output, slug)
                self.assertIn(f"UID:{original.uid}", active)
                self.assertIn(f"SEQUENCE:{old['sequence'] + 1}", active)
                self.assertIn("DTSTART:20260913T233000Z", active)
                self.assertIn("STATUS:CANCELLED", cancelled)
            saved = json.loads(first_state)
            self.assertEqual(saved[original.uid]["ranking_snapshot"], old["ranking_snapshot"])
            write_feeds([moved], output, path, NOW + timedelta(days=7))
            self.assertEqual(path.read_bytes(), first_state)
            self.assertNotIn(moved.uid, saved)

    def test_new_game_keeps_first_published_uid_after_multiple_date_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "state.json"
            original = parse_osu_schedule(schedule())[0]
            for revision, day in enumerate(("Sep 12 (Sat)", "Sep 13 (Sun)", "Sep 14 (Mon)")):
                game = parse_osu_schedule(schedule(day))[0]
                write_feeds([game], output, path, NOW + timedelta(days=revision))
                event, = events(output, "football")
                self.assertIn(f"UID:{original.uid}", event)
                self.assertIn(f"SEQUENCE:{revision}", event)

    def test_new_season_gets_distinct_uid(self):
        state = {}
        first = reconcile_osu_games(parse_osu_schedule(schedule()), state)[0]
        next_year = reconcile_osu_games(parse_osu_schedule(schedule(season=2027)), state)[0]
        self.assertNotEqual(first.uid, next_year.uid)

    def test_reschedule_across_new_year_and_venue_change_preserves_uid(self):
        original = parse_osu_schedule(schedule("Dec 31 (Thu)"))[0]
        state = {}
        reconcile_osu_games([original], state)
        moved = parse_osu_schedule(schedule("Jan 1 (Fri)").replace("Away", "Home"))
        resolved, = reconcile_osu_games(moved, state)
        self.assertEqual(resolved.uid, original.uid)
        self.assertEqual(resolved.game_date, date(2027, 1, 1))
        self.assertEqual(resolved.home_away, "home")

    def test_reschedule_preserves_completed_ranking_snapshot(self):
        state = json.loads((ROOT / "tests/fixtures/osu-published-state.json").read_text())
        original = next(game for game in parse_osu_schedule(OSU_HTML) if game.opponent == "USC")
        snapshot = state[original.uid]["ranking_snapshot"]
        self.assertIsNotNone(snapshot)
        moved = parse_osu_schedule(OSU_HTML.replace("Oct 31 (Sat)", "Nov 1 (Sun)"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "state.json"
            path.write_text(json.dumps(state))
            write_feeds(moved, output, path, NOW)
            saved = json.loads(path.read_text())
            self.assertEqual(saved[original.uid]["ranking_snapshot"], snapshot)
            event = next(event for event in events(output, "football") if f"UID:{original.uid}" in event)
            self.assertIn(f"SUMMARY:{snapshot['rank']} Ohio State Buckeyes at USC", event)

    def test_new_match_before_rescheduled_match_does_not_change_reconciliation(self):
        state = {}
        original = reconcile_osu_games(parse_osu_schedule(schedule()), state)[0]
        games = (parse_osu_schedule(schedule("Nov 28 (Sat)", "Michigan"))
                 + parse_osu_schedule(schedule("Sep 13 (Sun)")))
        forward = reconcile_osu_games(games, copy.deepcopy(state))
        backward = reconcile_osu_games(list(reversed(games)), copy.deepcopy(state))
        self.assertEqual({g.opponent: g.uid for g in forward}, {g.opponent: g.uid for g in backward})
        self.assertEqual(forward[1].uid, original.uid)

    def test_ambiguous_rematches_do_not_replace_published_output_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "state.json"
            games = parse_osu_schedule(schedule()) + parse_osu_schedule(schedule("Nov 28 (Sat)"))
            write_feeds(games, output, path, NOW)
            before = {file.name: file.read_bytes() for file in output.iterdir()}
            moved = parse_osu_schedule(schedule("Sep 13 (Sun)")) + parse_osu_schedule(schedule("Nov 29 (Sun)"))
            with self.assertRaisesRegex(ValueError, "Ambiguous Ohio State"):
                write_feeds(moved, output, path, NOW)
            self.assertEqual({file.name: file.read_bytes() for file in output.iterdir()}, before)

    def test_new_postseason_rematch_keeps_both_events_and_revisions_stable(self):
        for initial_day, current_day in (("Sep 12 (Sat)", "Sep 12 (Sat)"),
                                         ("Sep 12 (Sat)", "Sep 13 (Sun)")):
            with self.subTest(current_day=current_day), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                path = output / "state.json"
                original = parse_osu_schedule(schedule(initial_day))[0]
                write_feeds([original], output, path, NOW)
                regular = parse_osu_schedule(schedule(current_day))[0]
                write_feeds([regular], output, path, NOW)
                rematch = parse_osu_schedule(schedule("Jan 1 (Fri)"))[0]
                write_feeds([regular, rematch], output, path, NOW)
                match_events = events(output, "football")
                self.assertEqual(len(match_events), 2)
                self.assertIn(f"UID:{original.uid}", match_events[0])
                self.assertIn(f"UID:{rematch.uid}", match_events[1])
                self.assertNotEqual(original.uid, rematch.uid)
                first = path.read_bytes()
                write_feeds([rematch, regular], output, path, NOW + timedelta(days=1))
                self.assertEqual(path.read_bytes(), first)
                self.assertEqual(events(output, "football"), match_events)

    def test_rematch_on_original_date_cannot_steal_rescheduled_games_uid(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                path = output / "state.json"
                original = parse_osu_schedule(schedule())[0]
                write_feeds([original], output, path, NOW)
                moved = parse_osu_schedule(schedule("Sep 13 (Sun)"))[0]
                write_feeds([moved], output, path, NOW)
                games = [original, moved] if reverse else [moved, original]
                write_feeds(games, output, path, NOW)
                first, second = events(output, "football")
                self.assertNotIn(f"UID:{original.uid}\n", first)
                self.assertIn(f"UID:{original.uid}\n", second)
                self.assertIn("DTSTART:20260913T233000Z", second)
                saved = path.read_bytes()
                write_feeds(list(reversed(games)), output, path, NOW + timedelta(days=1))
                self.assertEqual(path.read_bytes(), saved)

    def test_unrecognized_opponent_rename_requires_reconciliation(self):
        state = {}
        reconcile_osu_games(parse_osu_schedule(schedule()), state)
        with self.assertRaisesRegex(ValueError, "Unresolved Ohio State"):
            reconcile_osu_games(parse_osu_schedule(schedule(opponent="Texas Longhorns")), state)

    def test_duplicate_rows_are_rejected(self):
        game = parse_osu_schedule(schedule())[0]
        with self.assertRaisesRegex(ValueError, "Duplicate Ohio State"):
            reconcile_osu_games([game, game], {})


class SharedMatchTests(unittest.TestCase):
    def test_completeness_counts_unique_games_not_duplicate_provider_rows(self):
        printable = ("<table><tr><td>September 12, 2026</td><td>7:30 PM</td><td>Away</td>"
                     "<td>Texas</td><td>Austin, Texas</td><td>ABC</td></tr></table>")
        for unique_count in (9, 17):
            def fetch_json(url):
                if "/schedule?" in url and "seasontype=2" in url:
                    rows = [nfl_event(str(week), week) for week in range(1, unique_count + 1)]
                    return {"events": rows + rows}
                return {}

            with self.subTest(unique_count=unique_count), \
                    patch("scripts.calendar_feed.fetch_text", side_effect=lambda url: printable if url.endswith("/print") else OSU_HTML), \
                    patch("scripts.calendar_feed.fetch_json", side_effect=fetch_json):
                if unique_count < 17:
                    with self.assertRaisesRegex(ValueError, "incomplete schedule"):
                        get_live_games(NOW.date())
                else:
                    games = get_live_games(NOW.date())
                    for team in TEAMS[1:]:
                        ours = [game for game in games if game.team == team.name]
                        self.assertEqual(len(ours), 17)
                        self.assertEqual(len({game.uid for game in ours}), 17)

    def games(self):
        payload = {"events": [nfl_event()]}
        away = parse_espn_schedule(TEAMS[1], payload)[0]
        home = parse_espn_schedule(TEAMS[2], payload)[0]
        away.playoff = "Record: 9-3"
        home.playoff = "Record: 8-4"
        return [away, home]

    def test_shared_match_has_one_identical_event_in_each_feed_and_stable_revisions(self):
        games = self.games()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "state.json"
            # Preserve a revision already seen by subscribers under the old writer.
            path.write_text(json.dumps({games[0].uid: {"sequence": 7, "modified": "old", "fingerprint": "legacy"}}))
            write_feeds(games, output, path, NOW)
            first_state = path.read_bytes()
            expected, = events(output, "football")
            self.assertIn("SEQUENCE:8", expected)
            self.assertIn("San Francisco 49ers:\\nRecord: 9-3", expected)
            self.assertIn("New England Patriots:\\nRecord: 8-4", expected)
            for slug in ("49ers", "patriots"):
                self.assertEqual(events(output, slug), [expected])
            self.assertEqual(events(output, "ohio-state"), [])
            self.assertEqual(json.loads((output / "health.json").read_text())["events"], 1)
            write_feeds(list(reversed(games)), output, path, NOW + timedelta(days=1))
            self.assertEqual(path.read_bytes(), first_state)
            self.assertEqual(events(output, "football"), [expected])
            games[0].playoff = "Record: 10-3"
            write_feeds(games, output, path, NOW + timedelta(days=2))
            for slug in ("49ers", "patriots", "football"):
                event, = events(output, slug)
                self.assertIn("SEQUENCE:9", event)
                self.assertIn("Record: 10-3", event)
            changed_state = path.read_bytes()
            write_feeds(games, output, path, NOW + timedelta(days=3))
            self.assertEqual(path.read_bytes(), changed_state)

    def test_exact_duplicate_provider_records_are_deduplicated(self):
        game = self.games()[0]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_feeds([game, copy.deepcopy(game)], output, output / "state.json", NOW)
            self.assertEqual(len(events(output, "football")), 1)
            self.assertEqual(len(events(output, "49ers")), 1)

    def test_conflicting_provider_dates_fail_before_any_output_is_written(self):
        games = self.games()
        games[1] = replace(games[1], game_date=date(2026, 12, 7))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "Conflicting provider records"):
                write_feeds(games, output, output / "state.json", NOW)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
