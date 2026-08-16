#!/usr/bin/env python3
"""Generate Apple Calendar-ready football subscriptions from public sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football"
OHIO_STATE_SCHEDULE = "https://ohiostatebuckeyes.com/sports/football/schedule/text"
CFP_FIRST_RELEASE = {2026: date(2026, 11, 3)}
PACIFIC = ZoneInfo("America/Los_Angeles")
EASTERN = ZoneInfo("America/New_York")
SEASON_TYPES = {"1": "Preseason", "2": "Regular Season", "3": "Postseason"}


@dataclass(frozen=True)
class Team:
    slug: str
    name: str
    league: str
    espn_id: str | None = None
    duration_minutes: int = 195


TEAMS = (
    Team("ohio-state", "Ohio State Buckeyes", "college-football", "194", 210),
    Team("49ers", "San Francisco 49ers", "nfl", "25"),
    Team("patriots", "New England Patriots", "nfl", "17"),
)


@dataclass
class Game:
    provider_id: str
    team: str
    league: str
    opponent: str
    home_away: str
    season: int
    season_type: str
    week: str
    start: datetime | None
    game_date: date
    time_confirmed: bool
    venue: str = ""
    location: str = ""
    broadcasts: tuple[str, ...] = ()
    url: str = ""
    neutral: bool = False
    rank: str | None = None
    opponent_rank: str | None = None
    rank_source: str | None = None
    playoff: str | None = None

    @property
    def uid(self) -> str:
        return f"football-calendar-{self.league}-{self.provider_id}@local"


class ScheduleTable(HTMLParser):
    """Minimal parser for the official Ohio State schedule's table rows."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cells: list[str] = []
        self._cell: list[str] | None = None
        self._in_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._in_row = True
            self._cells = []
        elif self._in_row and tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._in_row:
            if self._cells:
                self.rows.append(self._cells)
            self._in_row = False


def fetch_bytes(url: str) -> bytes:
    """Use curl because the official schedule site rejects Python's TLS client."""
    result = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "2", url],
        check=True, capture_output=True, timeout=45,
    )
    return result.stdout


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_bytes(url))


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8")


def current_nfl_season(today: date) -> int:
    return today.year - 1 if today.month <= 2 else today.year


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def first_desktop_link(links: Iterable[dict[str, Any]]) -> str:
    for link in links:
        if "desktop" in link.get("rel", []) and link.get("href", "").startswith("https://"):
            return str(link["href"])
    return ""


def get_broadcasts(competition: dict[str, Any]) -> tuple[str, ...]:
    names = []
    for broadcast in competition.get("broadcasts", []):
        name = broadcast.get("media", {}).get("shortName")
        if name and name not in names:
            names.append(str(name))
    return tuple(names)


def parse_espn_schedule(team: Team, payload: dict[str, Any]) -> list[Game]:
    games: list[Game] = []
    for event in payload.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors") or []
        ours = next((item for item in competitors if str(item.get("team", {}).get("id")) == team.espn_id), None)
        opponent = next((item for item in competitors if item is not ours), None)
        if not ours or not opponent:
            continue
        start = parse_iso(competition.get("date") or event.get("date"))
        known_time = bool(competition.get("timeValid", event.get("timeValid", False)))
        if not start:
            continue
        venue = competition.get("venue", {})
        address = venue.get("address", {})
        location = ", ".join(str(v) for v in (address.get("city"), address.get("state"), address.get("country")) if v)
        games.append(Game(
            provider_id=str(event["id"]), team=team.name, league=team.league,
            opponent=str(opponent["team"].get("displayName", "Opponent")),
            home_away=str(ours.get("homeAway", "neutral")).lower(),
            season=int(event.get("season", {}).get("year") or start.year),
            season_type=str(event.get("seasonType", {}).get("name") or SEASON_TYPES.get(str(event.get("season", {}).get("type")), "Season")),
            week=str(event.get("week", {}).get("text") or event.get("week", {}).get("number") or ""),
            start=start, game_date=start.astimezone(PACIFIC).date(), time_confirmed=known_time,
            venue=str(venue.get("fullName", "")), location=location,
            broadcasts=get_broadcasts(competition), url=first_desktop_link(event.get("links", [])),
            neutral=bool(competition.get("neutralSite", False)),
        ))
    return games


def parse_osu_schedule(html: str, year: int) -> list[Game]:
    table = ScheduleTable()
    table.feed(html)
    games: list[Game] = []
    for cells in table.rows:
        if len(cells) < 5 or not re.fullmatch(r"[A-Z][a-z]{2} \d{1,2} \([A-Za-z]{3}\)", cells[0]):
            continue
        month_day = re.sub(r" \([A-Za-z]{3}\)", "", cells[0])
        game_day = datetime.strptime(f"{year} {month_day}", "%Y %b %d").date()
        kickoff = cells[1].upper()
        start: datetime | None = None
        confirmed = kickoff not in {"TBA", "", "-"}
        if confirmed:
            if kickoff == "NOON":
                kickoff = "12:00 PM"
            try:
                local_time = datetime.strptime(kickoff, "%I:%M %p").time()
                start = datetime.combine(game_day, local_time, EASTERN).astimezone(UTC)
            except ValueError:
                confirmed = False
        home_away = cells[2].lower()
        opponent = cells[3].strip() or "Opponent TBD"
        raw_location = cells[4].strip()
        venue_match = re.search(r"\(([^)]+)\)", raw_location)
        venue = venue_match.group(1) if venue_match else ""
        location = re.sub(r"\s*\([^)]+\)", "", raw_location)
        stable_key = f"{year}|{game_day.isoformat()}|{opponent.lower()}|{home_away}"
        provider_id = "osu-" + hashlib.sha256(stable_key.encode()).hexdigest()[:16]
        games.append(Game(
            provider_id=provider_id, team="Ohio State Buckeyes", league="college-football",
            opponent=opponent, home_away=home_away, season=year, season_type="Regular Season",
            week="", start=start, game_date=game_day, time_confirmed=confirmed,
            venue=venue, location=location, url=OHIO_STATE_SCHEDULE,
        ))
    if not games:
        raise ValueError("Ohio State official schedule contained no parseable games")
    return games


def normalized_team_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def select_ranking(payload: dict[str, Any], today: date) -> tuple[dict[str, int], dict[str, int], str | None]:
    """Return only the requested AP/CFP poll; never quietly substitute Coaches."""
    desired = "CFP" if today >= CFP_FIRST_RELEASE.get(today.year, date(today.year, 11, 1)) else "AP"
    for ranking in payload.get("rankings", []):
        name = str(ranking.get("name", ""))
        valid = (desired == "AP" and name == "AP Top 25") or (desired == "CFP" and "College Football Playoff" in name)
        if valid:
            by_id, by_name = {}, {}
            for row in ranking.get("ranks", []):
                team, rank = row["team"], int(row["current"])
                by_id[str(team["id"])] = rank
                for label in (team.get("displayName"), team.get("location"), team.get("name"), team.get("abbreviation")):
                    if label:
                        by_name[normalized_team_name(str(label))] = rank
            return by_id, by_name, name
    return {}, {}, None


def walk_entries(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        if isinstance(node.get("entries"), list):
            yield from node["entries"]
        for value in node.values():
            yield from walk_entries(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_entries(value)


def playoff_status(team_id: str, standings: dict[str, Any], today: date) -> str | None:
    """Use provider-confirmed facts, never calculated clinching scenarios."""
    for entry in walk_entries(standings):
        if str(entry.get("team", {}).get("id")) != team_id:
            continue
        stats = {stat.get("name"): stat for stat in entry.get("stats", [])}
        clincher = stats.get("clincher", {})
        explicit = clincher.get("description")
        lines = []
        overall = stats.get("overall", {}).get("displayValue")
        seed = stats.get("playoffSeed", {}).get("displayValue")
        if overall:
            lines.append(f"Record: {overall}")
        if seed and seed not in {"0", "-"}:
            lines.append(f"Current playoff seed: No. {seed} (provisional)")
        if explicit:
            lines.append(f"Status: {explicit}")
        if not lines:
            return None
        return "\n".join([f"NFL playoff picture (updated {today.isoformat()}):", *lines])
    return None


def enrich(games: list[Game], rankings: dict[str, int], rankings_by_name: dict[str, int], ranking_source: str | None,
           standings: dict[str, Any], today: date) -> None:
    for game in games:
        if game.league == "college-football" and rankings:
            rank = rankings.get("194")
            if rank:
                game.rank, game.rank_source = f"#{rank}", ranking_source
            opponent_rank = rankings_by_name.get(normalized_team_name(game.opponent))
            if opponent_rank:
                game.opponent_rank, game.rank_source = f"#{opponent_rank}", ranking_source
        playoff_window = today <= game.game_date <= today + timedelta(days=28)
        if game.league == "nfl" and game.week and playoff_window:
            week_number = re.search(r"(\d+)$", game.week)
            if week_number and 13 <= int(week_number.group(1)) <= 18:
                team = next(team for team in TEAMS if team.name == game.team)
                game.playoff = playoff_status(str(team.espn_id), standings, today)


def text_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> list[str]:
    """RFC 5545 physical lines may not exceed 75 UTF-8 octets."""
    chunks, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if current and size + width > 75:
            chunks.append(current)
            current, size = " " + char, 1 + width
        else:
            current += char
            size += width
    return chunks + [current]


def state_for(game: Game) -> dict[str, Any]:
    return asdict(game)


def event_lines(game: Game, state: dict[str, Any], now: datetime) -> list[str]:
    old = state.get(game.uid, {})
    fingerprint = hashlib.sha256(json.dumps(state_for(game), sort_keys=True, default=str).encode()).hexdigest()
    if old.get("fingerprint") == fingerprint:
        sequence, modified = old["sequence"], old["modified"]
    else:
        sequence, modified = int(old.get("sequence", -1)) + 1, now.strftime("%Y%m%dT%H%M%SZ")
        state[game.uid] = {"fingerprint": fingerprint, "sequence": sequence, "modified": modified}
    team_name = f"{game.rank} {game.team}" if game.rank else game.team
    opponent_name = f"{game.opponent_rank} {game.opponent}" if game.opponent_rank else game.opponent
    summary = f"{opponent_name} at {team_name}" if game.home_away == "home" else f"{team_name} at {opponent_name}"
    if game.league == "nfl" and game.season_type.casefold() == "preseason":
        summary = f"Preseason: {summary}"
    if game.neutral:
        summary += " (Neutral site)"
    if not game.time_confirmed:
        summary += " — Kickoff TBD"
    details = [
        f"{game.season_type}{' • ' + game.week if game.week else ''}",
        f"{game.home_away.title()}{' • Neutral site' if game.neutral else ''}",
        f"Venue: {game.venue}" if game.venue else "",
        f"TV/stream: {', '.join(game.broadcasts)}" if game.broadcasts else "",
        f"Rankings: Ohio State {game.rank or 'Unranked'}; {game.opponent} {game.opponent_rank or 'Unranked'} ({game.rank_source})" if game.rank_source else "",
        game.playoff or "",
        "Kickoff is not confirmed; this all-day placeholder will become a timed event when announced." if not game.time_confirmed else "",
        "Source: " + game.url if game.url else "",
    ]
    lines = ["BEGIN:VEVENT", f"UID:{game.uid}", f"DTSTAMP:{modified}",
             f"LAST-MODIFIED:{modified}", f"SEQUENCE:{sequence}", f"SUMMARY:{text_value(summary)}",
             "TRANSP:TRANSPARENT", f"CATEGORIES:{text_value('Football,' + game.league + ',' + game.team)}"]
    if game.time_confirmed and game.start:
        lines.extend([f"DTSTART:{game.start.strftime('%Y%m%dT%H%M%SZ')}",
                      f"DTEND:{(game.start + timedelta(minutes=next(t.duration_minutes for t in TEAMS if t.name == game.team))).strftime('%Y%m%dT%H%M%SZ')}"])
    else:
        lines.extend([f"DTSTART;VALUE=DATE:{game.game_date.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(game.game_date + timedelta(days=1)).strftime('%Y%m%d')}"])
    if game.location or game.venue:
        lines.append(f"LOCATION:{text_value(', '.join(x for x in (game.venue, game.location) if x))}")
    if game.url:
        lines.append(f"URL:{game.url}")
    lines.append("DESCRIPTION:" + text_value("\n".join(line for line in details if line)))
    return lines + ["END:VEVENT"]


def render_calendar(name: str, games: list[Game], state: dict[str, Any], now: datetime) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Football Calendars//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{text_value(name)}", "X-WR-TIMEZONE:America/Los_Angeles",
             "X-PUBLISHED-TTL:PT1H"]
    for game in sorted(games, key=lambda game: (game.game_date, game.provider_id)):
        lines.extend(event_lines(game, state, now))
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in fold(line)) + "\r\n"


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def write_feeds(games: list[Game], output: Path, state_path: Path, now: datetime) -> None:
    output.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    grouped = {team.slug: [game for game in games if game.team == team.name] for team in TEAMS}
    for team in TEAMS:
        (output / f"{team.slug}.ics").write_text(render_calendar(team.name, grouped[team.slug], state, now), newline="")
    (output / "football.ics").write_text(render_calendar("Football", games, state, now), newline="")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    (output / "health.json").write_text(json.dumps({"generatedAt": now.isoformat(), "events": len(games), "source": "live"}, indent=2) + "\n")


def require_complete_schedule(label: str, games: list[Game], minimum: int) -> None:
    if len(games) < minimum:
        raise ValueError(f"{label} provider returned an incomplete schedule")


def get_live_games(today: date) -> list[Game]:
    osu = parse_osu_schedule(fetch_text(OHIO_STATE_SCHEDULE), today.year)
    require_complete_schedule("Ohio State", osu, 10)
    nfl_games: list[Game] = []
    nfl_season = current_nfl_season(today)
    for team in TEAMS[1:]:
        payload = fetch_json(f"{ESPN}/nfl/scoreboard?dates={nfl_season}0801-{nfl_season + 1}0220&limit=1000")
        parsed = parse_espn_schedule(team, payload)
        regular_season_games = [game for game in parsed if game.season_type == "Regular Season"]
        require_complete_schedule(f"NFL provider ({team.name} regular season)", regular_season_games, 17)
        nfl_games.extend(parsed)
    ranking_payload = fetch_json(f"{ESPN}/college-football/rankings")
    ranks, ranks_by_name, source = select_ranking(ranking_payload, today)
    standings = fetch_json(f"https://site.api.espn.com/apis/v2/sports/football/nfl/standings?season={current_nfl_season(today)}&seasontype=2")
    games = osu + nfl_games
    enrich(games, ranks, ranks_by_name, source, standings, today)
    return games


def load_fixture(path: Path) -> list[Game]:
    raw = json.loads(path.read_text())
    games = []
    for item in raw["games"]:
        item["start"] = parse_iso(item.get("start"))
        item["game_date"] = date.fromisoformat(item["game_date"])
        item["broadcasts"] = tuple(item.get("broadcasts", []))
        games.append(Game(**item))
    return games


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("data/feed-state.json"))
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        games = load_fixture(args.fixture) if args.fixture else get_live_games(now.date())
        if not games:
            raise ValueError("no games were produced")
        write_feeds(games, args.output, args.state, now)
    except Exception as error:
        print(f"calendar refresh failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
