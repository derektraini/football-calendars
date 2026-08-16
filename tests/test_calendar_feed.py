from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.calendar_feed import Game, event_lines, load_fixture, require_complete_schedule, write_feeds


ROOT = Path(__file__).parent


class CalendarFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.games = load_fixture(ROOT / "fixtures" / "sample.json")
        self.now = datetime(2026, 8, 16, 18, 0, tzinfo=UTC)

    def test_team_and_combined_feeds_are_validly_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            state = Path(directory) / "state.json"
            write_feeds(self.games, output, state, self.now)
            expected_counts = {"ohio-state": 1, "49ers": 1, "patriots": 1, "football": 3}
            for feed, count in expected_counts.items():
                text = (output / f"{feed}.ics").read_bytes().decode("utf-8")
                self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
                self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
                self.assertEqual(text.count("BEGIN:VEVENT"), count)
                self.assertEqual(text.count("END:VEVENT"), count)
                for physical_line in text.split("\r\n"):
                    self.assertLessEqual(len(physical_line.encode("utf-8")), 75)

    def test_tbd_is_all_day_and_confirmed_game_is_timed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            write_feeds(self.games, output, Path(directory) / "state.json", self.now)
            patriots = (output / "patriots.ics").read_text()
            self.assertIn("DTSTART;VALUE=DATE:20261227", patriots)
            self.assertIn("DTEND;VALUE=DATE:20261228", patriots)
            self.assertIn("Kickoff TBD", patriots)
            niners = (output / "49ers.ics").read_text()
            self.assertIn("DTSTART:20261206T212500Z", niners)
            self.assertIn("DTEND:20261207T004000Z", niners)
            self.assertIn("Status: Clinched Division", niners)

    def test_uid_is_stable_and_sequence_changes_only_for_material_change(self) -> None:
        state: dict[str, object] = {}
        game = self.games[0]
        first = event_lines(game, state, self.now)
        unchanged = event_lines(game, state, self.now + timedelta(days=7))
        shifted = Game(**{**game.__dict__, "start": game.start + timedelta(hours=1)})
        changed = event_lines(shifted, state, self.now + timedelta(days=14))
        self.assertEqual(next(line for line in first if line.startswith("UID:")), next(line for line in unchanged if line.startswith("UID:")))
        self.assertIn("SEQUENCE:0", first)
        self.assertIn("SEQUENCE:0", unchanged)
        self.assertIn("SEQUENCE:1", changed)

    def test_playoff_update_is_a_material_event_revision(self) -> None:
        state: dict[str, object] = {}
        game = self.games[1]
        event_lines(game, state, self.now)
        revised = Game(**{**game.__dict__, "playoff": game.playoff + "\nStatus: Clinched Playoffs"})
        lines = event_lines(revised, state, self.now + timedelta(days=7))
        self.assertIn("SEQUENCE:1", lines)

    def test_nfl_preseason_is_labeled_in_the_event_title(self) -> None:
        preseason = Game(**{**self.games[1].__dict__, "season_type": "Preseason"})
        lines = event_lines(preseason, {}, self.now)
        self.assertIn("SUMMARY:Preseason: Seattle Seahawks at San Francisco 49ers", lines)

    def test_incomplete_schedule_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ohio State provider returned an incomplete schedule"):
            require_complete_schedule("Ohio State", self.games[:1], 10)

    def test_fixture_is_json_safe(self) -> None:
        raw = json.loads((ROOT / "fixtures" / "sample.json").read_text())
        self.assertEqual(len(raw["games"]), 3)


if __name__ == "__main__":
    unittest.main()
