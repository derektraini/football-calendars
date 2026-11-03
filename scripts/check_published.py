#!/usr/bin/env python3
"""Check the public calendar publication without changing feeds or state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://derektraini.github.io/football-calendars"
CALENDARS = ("ohio-state.ics", "49ers.ics", "patriots.ics", "football.ics")
MAX_AGE_HOURS = 8 * 24


def fetch_bytes(url: str) -> bytes:
    try:
        result = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location",
             "--connect-timeout", "5", "--max-time", "15", "--retry", "1",
             "--header", "Cache-Control: no-cache", url],
            check=True, capture_output=True, timeout=35,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"Request timed out: {url}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Request failed for {url}: {detail}") from error
    return result.stdout


def calendar_events(raw: bytes, filename: str) -> dict[str, bool]:
    """Return UID -> cancelled for the generator's UTC/all-day calendar format."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{filename}: calendar is not UTF-8") from error
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise ValueError(f"{filename}: missing calendar boundaries")
    events: dict[str, bool] = {}
    event: dict[str, str] | None = None
    version = False
    for line in lines[1:-1]:
        if line == "BEGIN:VEVENT" and event is None:
            event = {}
        elif line == "END:VEVENT" and event is not None:
            uid = event.get("UID", "")
            if not uid.strip():
                raise ValueError(f"{filename}: event has no UID")
            if uid in events:
                raise ValueError(f"{filename}: duplicate UID {uid}")
            if "DTSTART" not in event or "DTSTAMP" not in event:
                raise ValueError(f"{filename}: event {uid} has no DTSTART or DTSTAMP")
            events[uid] = event.get("STATUS") == "CANCELLED"
            event = None
        elif line.startswith(("BEGIN:", "END:")):
            raise ValueError(f"{filename}: unexpected component boundary {line}")
        else:
            name, separator, value = line.partition(":")
            if not separator:
                raise ValueError(f"{filename}: malformed calendar property")
            key = name.split(";", 1)[0]
            if event is not None and key in {"UID", "STATUS", "DTSTART", "DTSTAMP"}:
                if key in event or not value.strip():
                    raise ValueError(f"{filename}: missing or repeated {key}")
                if key in {"DTSTART", "DTSTAMP"}:
                    all_day = name == "DTSTART;VALUE=DATE"
                    pattern = r"\d{8}" if all_day else r"\d{8}T\d{6}Z"
                    date_format = "%Y%m%d" if all_day else "%Y%m%dT%H%M%SZ"
                    if (name != key and not all_day) or not re.fullmatch(pattern, value):
                        raise ValueError(f"{filename}: invalid {key} date format")
                    try:
                        datetime.strptime(value, date_format)
                    except ValueError as error:
                        raise ValueError(f"{filename}: invalid {key} date") from error
                event[key] = value
            elif event is None and line == "VERSION:2.0":
                version = True
    if event is not None or not version:
        raise ValueError(f"{filename}: incomplete calendar")
    if not any(not cancelled for cancelled in events.values()):
        raise ValueError(f"{filename}: no active events")
    return events


def check_publication(health: object, calendars: dict[str, bytes], now: datetime,
                      max_age: timedelta = timedelta(hours=MAX_AGE_HOURS)) -> str:
    if not isinstance(health, dict) or health.get("source") != "live":
        raise ValueError("health.json: source must be live; fixture output must not be published")
    try:
        generated = datetime.fromisoformat(health["generatedAt"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("health.json: invalid generatedAt timestamp") from error
    if generated.utcoffset() is None:
        raise ValueError("health.json: generatedAt must include a timezone")
    age = now - generated
    if age < -timedelta(minutes=5):
        raise ValueError("health.json: generatedAt is more than five minutes in the future")
    if age > max_age:
        raise ValueError(f"Publication is stale: generated {generated.isoformat()}, "
                         f"{age.total_seconds() / 3600:.1f} hours ago "
                         f"(limit {max_age.total_seconds() / 3600:g} hours)")
    count = health.get("events")
    if type(count) is not int or count <= 0:
        raise ValueError("health.json: events must be a positive integer")
    if set(calendars) != set(CALENDARS):
        raise ValueError("Publication must contain all four calendars")
    if "files" in health:
        manifest = health["files"]
        if not isinstance(manifest, dict) or set(manifest) != set(CALENDARS):
            raise ValueError("health.json: files must contain all four calendar hashes")
        for filename, raw in calendars.items():
            digest = manifest[filename]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"health.json: invalid SHA256 for {filename}")
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError(f"{filename}: checksum mismatch; health and calendar versions differ")
    parsed = {filename: calendar_events(raw, filename) for filename, raw in calendars.items()}
    combined = parsed["football.ics"]
    team_events: dict[str, bool] = {}
    for filename in CALENDARS[:-1]:
        for uid, cancelled in parsed[filename].items():
            if uid in team_events and team_events[uid] != cancelled:
                raise ValueError(f"{filename}: conflicting cancellation status for {uid}")
            team_events[uid] = cancelled
    if team_events != combined:
        raise ValueError("Team calendars and combined calendar disagree on UIDs or cancellations")
    active = sum(not cancelled for cancelled in combined.values())
    if active != count:
        raise ValueError(f"health.json reports {count} active events; combined calendar contains {active}")
    checksums = ("All four file checksums verified." if "files" in health else
                 "Legacy health format: checksum verification unavailable until the next refresh.")
    return (f"Published calendars healthy: {active} active events; retained cancellations: {len(combined) - active}.\n"
            f"Generated {generated.isoformat()} ({max(age.total_seconds(), 0) / 3600:.1f} hours ago).\n"
            f"{checksums}")


def check_site(base_url: str, max_age: timedelta) -> str:
    # A deployment can briefly serve files from two versions. Refetch the whole
    # publication once so a transient mismatch does not produce an alert.
    for attempt in range(2):
        try:
            health = json.loads(fetch_bytes(f"{base_url}/health.json"))
            calendars = {filename: fetch_bytes(f"{base_url}/{filename}") for filename in CALENDARS}
            return check_publication(health, calendars, datetime.now(UTC), max_age)
        except ValueError:
            if attempt:
                raise
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-age-hours", type=int, default=MAX_AGE_HOURS)
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")
    url = urlparse(base_url)
    if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
        parser.error("--base-url must be an HTTP(S) URL without a query or fragment")
    if args.max_age_hours <= 0:
        parser.error("--max-age-hours must be positive")
    try:
        print(check_site(base_url, timedelta(hours=args.max_age_hours)))
    except (ValueError, OSError) as error:
        print(f"Published calendar check failed: {error}\n"
              "Inspect the Refresh calendars and Deploy calendar feeds workflow logs. "
              "Verify the public Pages files before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
