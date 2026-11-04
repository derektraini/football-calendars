from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from scripts.calendar_feed import load_fixture, main, write_feeds


FIXTURE = Path(__file__).parent / "fixtures" / "sample.json"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class DryRunTests(unittest.TestCase):
    def test_write_feeds_dry_run_writes_nothing_and_reports_counts(self) -> None:
        games = load_fixture(FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site"
            state = Path(tmp) / "state.json"
            health = write_feeds(games, output, state, NOW, source="fixture", dry_run=True)
            self.assertFalse(output.exists(), "dry run must not create the output directory")
            self.assertFalse(state.exists(), "dry run must not write state")
            self.assertEqual(health["source"], "fixture")
            self.assertGreater(health["events"], 0)
            self.assertIn("ohio-state.ics", health["files"])
            self.assertIn("football.ics", health["files"])

    def test_write_feeds_normal_run_still_writes(self) -> None:
        games = load_fixture(FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site"
            state = Path(tmp) / "state.json"
            health = write_feeds(games, output, state, NOW, source="fixture")
            self.assertTrue((output / "football.ics").exists())
            self.assertTrue(state.exists())
            self.assertGreater(health["events"], 0)
            self.assertEqual(json.loads((output / "health.json").read_text())["events"], health["events"])

    def test_main_dry_run_with_fixture_prints_summary_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main_for_test(["--output", str(output), "--fixture", str(FIXTURE), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertFalse(output.exists())
            self.assertIn("dry run", stdout.getvalue())
            self.assertIn("football.ics", stdout.getvalue())


def main_for_test(argv: list[str]) -> int:
    import sys

    old = sys.argv
    try:
        sys.argv = ["calendar_feed.py", *argv]
        return main()
    finally:
        sys.argv = old


if __name__ == "__main__":
    unittest.main()
