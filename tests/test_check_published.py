from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.calendar_feed import load_fixture, write_feeds
from scripts.check_published import (
    CALENDARS, MAX_AGE_HOURS, check_publication, check_site, fetch_bytes, main,
)

NOW = datetime(2026, 11, 3, 18, tzinfo=UTC)
MAX_AGE = timedelta(hours=MAX_AGE_HOURS)


def calendar(*uids, cancelled=()):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for uid in (*uids, *cancelled):
        lines += ["BEGIN:VEVENT", f"UID:{uid}", "DTSTART:20261107T200000Z",
                  "DTSTAMP:20260101T000000Z", "SEQUENCE:4"]
        if uid in cancelled:
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    return ("\r\n".join([*lines, "END:VCALENDAR"]) + "\r\n").encode()


def publication():
    return ({"source": "live", "generatedAt": (NOW - timedelta(days=6)).isoformat(), "events": 3}, {
        "ohio-state.ics": calendar("osu", cancelled=("old-osu",)),
        "49ers.ics": calendar("sf"),
        "patriots.ics": calendar("ne"),
        "football.ics": calendar("osu", "sf", "ne", cancelled=("old-osu",)),
    })


def with_hashes(health, files):
    return {**health, "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}


class PublishedCalendarTests(unittest.TestCase):
    def test_current_legacy_format_accepts_cancellations_and_old_event_timestamps(self):
        health, files = publication()
        result = check_publication(health, files, NOW)
        self.assertIn("3 active events; retained cancellations: 1", result)
        self.assertIn("checksum verification unavailable", result)

    def test_exact_age_limit_passes_and_one_second_over_fails(self):
        health, files = publication()
        health["generatedAt"] = (NOW - MAX_AGE).isoformat()
        check_publication(health, files, NOW)
        with self.assertRaisesRegex(ValueError, "stale"):
            check_publication(health, files, NOW + timedelta(seconds=1))

    def test_timezone_offsets_and_small_clock_skew_are_accepted(self):
        health, files = publication()
        health["generatedAt"] = "2026-11-03T10:05:00-08:00"
        check_publication(health, files, NOW)
        health["generatedAt"] = "2026-11-03T10:05:01-08:00"
        with self.assertRaisesRegex(ValueError, "future"):
            check_publication(health, files, NOW)

    def test_fixture_invalid_timestamps_and_invalid_counts_are_rejected(self):
        health, files = publication()
        cases = [({"source": "fixture"}, "source must be live"),
                 ({"generatedAt": "not a date"}, "timestamp"),
                 ({"generatedAt": None}, "timestamp"),
                 ({"generatedAt": "2026-11-03T17:00:00"}, "timezone"),
                 ({"events": True}, "positive integer"),
                 ({"events": 0}, "positive integer"),
                 ({"events": "3"}, "positive integer"),
                 ({"events": 4}, "combined calendar contains 3")]
        for update, error in cases:
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, error):
                check_publication({**health, **update}, files, NOW)
        with self.assertRaises(ValueError):
            check_publication([], files, NOW)

    def test_all_four_files_are_required(self):
        health, files = publication()
        del files["patriots.ics"]
        with self.assertRaisesRegex(ValueError, "all four"):
            check_publication(health, files, NOW)

    def test_html_empty_and_truncated_calendars_are_rejected(self):
        health, files = publication()
        invalid = (b"<html>Service unavailable</html>", calendar(),
                   calendar("osu")[:-25], calendar("osu").replace(b"END:VEVENT\r\n", b""),
                   calendar("osu").replace(b"VERSION:2.0", b"VERSION:1.0"),
                   calendar("osu").replace(b"BEGIN:VEVENT", b"END:VEVENT", 1),
                   calendar("osu").replace(b"UID:osu", b"UID:osu\r\nUID:another"),
                   calendar("osu").replace(b"DTSTART:20261107T200000Z\r\n", b""))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                check_publication(health, {**files, "ohio-state.ics": raw}, NOW)

    def test_duplicate_uids_including_cancellations_are_rejected(self):
        health, files = publication()
        for raw in (calendar("osu", "osu"), calendar("osu", cancelled=("osu",))):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "duplicate UID"):
                check_publication(health, {**files, "ohio-state.ics": raw}, NOW)

    def test_all_day_dates_are_accepted_and_invalid_dates_fail_even_with_valid_hashes(self):
        health, files = publication()
        files["ohio-state.ics"] = files["ohio-state.ics"].replace(
            b"DTSTART:20261107T200000Z", b"DTSTART;VALUE=DATE:20261107")
        check_publication(with_hashes(health, files), files, NOW)
        for old, new in ((b"DTSTART;VALUE=DATE:20261107", b"DTSTART:not-a-date"),
                         (b"DTSTAMP:20260101T000000Z", b"DTSTAMP:also-not-a-date"),
                         (b"DTSTART;VALUE=DATE:20261107", b"DTSTART;VALUE=DATE:20260230"),
                         (b"DTSTART;VALUE=DATE:20261107", b"DTSTART:20261107T250000Z"),
                         (b"DTSTART;VALUE=DATE:20261107", b"DTSTART:20261107")):
            changed = {**files, "ohio-state.ics": files["ohio-state.ics"].replace(old, new)}
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, "invalid DT"):
                check_publication(with_hashes(health, changed), changed, NOW)

    def test_folded_properties_are_unfolded_before_uid_comparison(self):
        health, files = publication()
        files["ohio-state.ics"] = files["ohio-state.ics"].replace(b"UID:old-osu", b"UID:old-\r\n osu")
        check_publication(health, files, NOW)

    def test_shared_nfl_uid_is_allowed_across_team_feeds(self):
        health, files = publication()
        health["events"] = 2
        files["49ers.ics"] = files["patriots.ics"] = calendar("shared-nfl")
        files["football.ics"] = calendar("osu", "shared-nfl", cancelled=("old-osu",))
        check_publication(health, files, NOW)

    def test_team_union_must_equal_combined_including_cancellation_status(self):
        health, files = publication()
        for combined in (calendar("osu", "sf", "ne", "unexpected", cancelled=("old-osu",)),
                         calendar("osu", "sf", cancelled=("old-osu",)),
                         calendar("osu", "sf", "ne", "old-osu")):
            with self.subTest(combined=combined), self.assertRaisesRegex(ValueError, "disagree"):
                check_publication(health, {**files, "football.ics": combined}, NOW)

    def test_manifest_verifies_exact_bytes_including_line_endings(self):
        health, files = publication()
        health = with_hashes(health, files)
        self.assertIn("All four file checksums verified", check_publication(health, files, NOW))
        files["49ers.ics"] = files["49ers.ics"].replace(b"\r\n", b"\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            check_publication(health, files, NOW)

    def test_present_but_incomplete_or_invalid_manifest_is_rejected(self):
        health, files = publication()
        valid = with_hashes(health, files)["files"]
        for manifest in (None, {}, {"49ers.ics": valid["49ers.ics"]},
                         {**valid, "49ers.ics": "invalid"}, {**valid, "football.ics": 1}):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, "health.json"):
                check_publication({**health, "files": manifest}, files, NOW)

    def test_generator_hashes_match_written_calendars(self):
        fixture = Path(__file__).parent / "fixtures/sample.json"
        games = load_fixture(fixture)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_feeds(games, output, output / "state.json", NOW, source="live")
            health = json.loads((output / "health.json").read_text())
            files = {name: (output / name).read_bytes() for name in CALENDARS}
            self.assertIn("All four file checksums verified", check_publication(health, files, NOW))


class MonitorCommandTests(unittest.TestCase):
    def test_publication_is_refetched_once_after_a_version_mismatch(self):
        health, files = publication()
        health["generatedAt"] = datetime.now(UTC).isoformat()
        health = with_hashes(health, files)
        mismatched = copy.deepcopy(files)
        mismatched["49ers.ics"] = calendar("stale")
        responses = ([json.dumps(health).encode(), *[mismatched[name] for name in CALENDARS]]
                     + [json.dumps(health).encode(), *[files[name] for name in CALENDARS]])
        with patch("scripts.check_published.fetch_bytes", side_effect=responses) as fetch:
            result = check_site("https://example.test/calendars", MAX_AGE)
        self.assertEqual(fetch.call_count, 10)
        self.assertIn("checksums verified", result)

    def test_persistent_failures_return_nonzero_with_actionable_message(self):
        with patch("scripts.check_published.fetch_bytes", side_effect=ValueError("HTTP 404")) as fetch, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main([]), 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("HTTP 404", stderr.getvalue())
        self.assertIn("workflow logs", stderr.getvalue())

    def test_network_timeout_is_reported(self):
        with patch("scripts.check_published.subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 35)):
            with self.assertRaisesRegex(ValueError, "timed out"):
                fetch_bytes("https://example.test/health.json")

    def test_http_error_is_reported(self):
        error = subprocess.CalledProcessError(22, "curl", stderr=b"HTTP 404")
        with patch("scripts.check_published.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(ValueError, "HTTP 404"):
                fetch_bytes("https://example.test/health.json")

    def test_success_returns_zero(self):
        with patch("scripts.check_published.check_site", return_value="healthy") as check, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["--base-url", "https://example.test/feeds/", "--max-age-hours", "24"]), 0)
        check.assert_called_once_with("https://example.test/feeds", timedelta(hours=24))
        self.assertEqual(stdout.getvalue(), "healthy\n")

    def test_invalid_monitor_configuration_is_rejected(self):
        for args in (["--max-age-hours", "0"], ["--max-age-hours", "-1"],
                     ["--base-url", "file:///tmp/site"], ["--base-url", "https://example.test/?query=1"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as raised:
                main(args)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
