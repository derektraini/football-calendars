from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from scripts.calendar_feed import Game, deduplicate_games, superseded_event_lines

NOW = datetime(2026, 11, 3, 18, tzinfo=UTC)


def nfl_game(team: str, opponent: str, home_away: str, **overrides) -> Game:
    fields = dict(
        provider_id="401",
        team=team,
        league="nfl",
        opponent=opponent,
        home_away=home_away,
        season=2026,
        season_type="Regular Season",
        week="Week 10",
        start=datetime(2026, 11, 8, 18, tzinfo=UTC),
        game_date=date(2026, 11, 8),
        time_confirmed=True,
        venue="Shared Stadium",
    )
    fields.update(overrides)
    return Game(**fields)


def perspectives(**overrides):
    """The same shared NFL game seen by each followed team's feed."""
    home = nfl_game("Buffalo Bills", "San Francisco 49ers", "home", **overrides)
    away = nfl_game("San Francisco 49ers", "Buffalo Bills", "away", **overrides)
    return home, away


class CanonicalGamesTests(unittest.TestCase):
    def test_shared_game_appears_once_and_prefers_home_perspective(self):
        home, away = perspectives()
        result = deduplicate_games([away, home])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].team, "Buffalo Bills")
        self.assertEqual(result[0].home_away, "home")

    def test_conflicting_provider_records_raise(self):
        home, _ = perspectives()
        conflicting = nfl_game("San Francisco 49ers", "Buffalo Bills", "away",
                               venue="A Different Stadium")
        with self.assertRaises(ValueError):
            deduplicate_games([home, conflicting])

    def test_non_opposing_duplicate_uid_raises(self):
        home, _ = perspectives()
        also_home = nfl_game("San Francisco 49ers", "Buffalo Bills", "home")
        with self.assertRaises(ValueError):
            deduplicate_games([home, also_home])

    def test_playoff_notes_from_both_perspectives_are_merged(self):
        home = nfl_game("Buffalo Bills", "San Francisco 49ers", "home",
                        playoff="Clinched the division")
        away = nfl_game("San Francisco 49ers", "Buffalo Bills", "away",
                        playoff="Eliminated from contention")
        result = deduplicate_games([away, home])
        self.assertEqual(len(result), 1)
        self.assertIn("Buffalo Bills:\nClinched the division", result[0].playoff)
        self.assertIn("San Francisco 49ers:\nEliminated from contention", result[0].playoff)

    def test_distinct_games_pass_through(self):
        home, _ = perspectives()
        other = nfl_game("Buffalo Bills", "Miami Dolphins", "home", provider_id="402")
        self.assertEqual(len(deduplicate_games([home, other])), 2)


class SupersededEventTests(unittest.TestCase):
    def setUp(self):
        self.game = Game(
            provider_id="osu-1", team="Ohio State", league="college-football",
            opponent="Texas", home_away="away", season=2026,
            season_type="Regular Season", week="", start=None,
            game_date=date(2026, 9, 12), time_confirmed=True,
        )
        self.uid = "football-calendar-college-football-superseded@local"

    def make_state(self):
        return {self.uid: {"fingerprint": "original", "sequence": 3,
                           "modified": "20260101T000000Z"}}

    def test_first_retirement_bumps_sequence_once_and_cancels(self):
        state = self.make_state()
        lines = superseded_event_lines(self.game, self.uid, state, NOW)
        self.assertIn(f"UID:{self.uid}", lines)
        self.assertIn("SEQUENCE:4", lines)
        self.assertIn("STATUS:CANCELLED", lines)
        self.assertEqual(state[self.uid]["sequence"], 4)
        self.assertEqual(state[self.uid]["fingerprint"],
                         f"superseded-by:{self.game.uid}")

    def test_repeat_retirement_is_stable(self):
        state = self.make_state()
        first = superseded_event_lines(self.game, self.uid, state, NOW)
        second = superseded_event_lines(self.game, self.uid, state, NOW)
        self.assertEqual(first, second)
        self.assertEqual(state[self.uid]["sequence"], 4)


if __name__ == "__main__":
    unittest.main()
