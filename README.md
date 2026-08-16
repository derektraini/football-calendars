# Football calendars

Free, auto-updating Apple Calendar subscriptions for the Ohio State Buckeyes,
San Francisco 49ers, and New England Patriots.

The generator writes standard iCalendar (`.ics`) feeds. It is deliberately a
one-way, read-only subscription: no Apple Calendar, iCloud, or personal data is
read or stored.

## What each event contains

- matchup, home/away/neutral designation, season type, and week
- a confirmed kickoff in UTC, or an all-day **Kickoff TBD** placeholder when a
  date is known but no time has been announced
- stadium/city, broadcaster when published, and a game/source link
- Ohio State's and its opponent's selected AP or CFP ranks when that exact poll is available
- for NFL games in Weeks 13–18, a conservative playoff picture: current record,
  conference seed, and only provider-confirmed clinches/eliminations

The NFL block never invents a “win-and-in”, magic number, or clinching scenario.
Those require full tiebreaker/scenario data. A definitive clincher is shown only
when the standings provider supplies one.

## Team calendar colors

Each individual feed includes the standard iCalendar `COLOR` hint. Use these same
values when selecting a custom calendar color in Apple Calendar:

| Calendar | Team color | Hex |
| --- | --- | --- |
| Ohio State | Scarlet | `#BB0000` |
| 49ers | Gold | `#B3995D` |
| Patriots | Navy | `#002244` |

Apple Calendar may retain the color you chose locally for an existing subscription;
that local choice takes precedence over the feed hint. The combined feed deliberately
has no color because it contains all three teams.

## Data sources and safeguards

- Ohio State schedule: the official [Ohio State text schedule](https://ohiostatebuckeyes.com/sports/football/schedule/text)
- NFL schedules and standings: public ESPN site data, isolated behind adapters
  because it is a convenient source but not a contractual public API
- Every event also links back to its source/game page.

The refresh job refuses to replace a previously generated feed with an empty
schedule. It stores event fingerprints in `data/feed-state.json`, keeping UIDs
stable and incrementing `SEQUENCE` only after a material event change.

## Publishing

1. Create a **public** GitHub repository from this folder and push it.
2. In **Settings → Pages**, set Source to **GitHub Actions**.
3. Run **Refresh calendars** once from the Actions tab. It generates the feeds
   and deploys the `site/` directory in the same run.
4. Replace the placeholders below with the repository's Pages address:

   ```text
   https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY/ohio-state.ics
   https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY/49ers.ics
   https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY/patriots.ics
   https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY/football.ics
   ```

Subscribe to one team feed *or* the combined feed—not both, or games will be
duplicated. In macOS Calendar choose **File → New Calendar Subscription**, paste
the URL, and select **iCloud** as the location. On iPhone/iPad choose
**Calendars → Add Calendar → Add Subscription Calendar**. Apple controls device
polling; set an automatic refresh cadence on the Mac and use **View → Refresh
Calendars** if a late flex change needs to appear immediately.

## Refresh schedule

`.github/workflows/refresh.yml` runs every Wednesday at 17:23 UTC (not at the
top of the hour, where GitHub says scheduled jobs are more likely to be delayed)
and also offers a manual run. That is 10:23 AM PDT / 9:23 AM PST. GitHub cron is
UTC, so one fixed weekly time cannot remain the same Pacific wall-clock time
through daylight-saving changes.

## Local verification

No runtime dependencies are needed beyond Python 3.11+.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/calendar_feed.py --fixture tests/fixtures/sample.json --output /tmp/football-calendars
```

The checked-in fixture is only for tests; it is never published. A normal refresh
uses live sources:

```sh
python3 scripts/calendar_feed.py --output site
```

## Ranking policy

Before the first published CFP ranking, the feed seeks a poll explicitly named
`AP Top 25`. On/after the CFP release window it seeks an explicitly named CFP
poll. If that exact poll is unavailable, the rank is omitted rather than silently
substituting the Coaches poll. The `ranking_provider` adapter is intentionally
small so it can be replaced with a licensed/official structured source if needed.
