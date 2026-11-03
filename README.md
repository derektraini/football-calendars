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
- Missing upcoming Ohio State kickoffs and broadcasts: the official [printable schedule](https://ohiostatebuckeyes.com/sports/football/schedule/print), matched by date, unranked opponent, and home/away. Confirmed text-schedule times take precedence; ambiguous or unconfirmed printable rows stay TBD. An unavailable or unparseable fallback fails the refresh before any feeds are written. Historical games are left unchanged.
- NFL schedules and standings: public ESPN site data, isolated behind adapters
  because it is a convenient source but not a contractual public API
- Every event also links back to its source/game page.

The refresh job refuses to replace a previously generated feed with an empty
schedule. It stores event fingerprints in `data/feed-state.json`, keeping UIDs
stable and incrementing `SEQUENCE` only after a material event change.

Ohio State dates use the season in the official schedule heading, with January
and February games falling in the following calendar year. Missing or conflicting
season headings stop the refresh. Published Ohio State state also records season
and normalized opponent so a date or venue change can retain its UID. Each
refresh also remembers the latest schedule row identifier, allowing a new
postseason rematch when the earlier meeting is still present, even after that
meeting was rescheduled.
The existing 2026 events have this metadata seeded without resetting revisions
or ranking snapshots. Keep the state file when moving or restoring the generator.
An ambiguous rematch or an unrecognized opponent rename stops generation for
explicit identity reconciliation instead of replacing a subscriber's event.

When both followed NFL teams play each other, each team feed and the combined
feed use the same event and revision. The home team's perspective is used, and
available playoff information is labeled for both teams. The combined feed
contains the match only once; conflicting provider records stop the refresh.
The NFL completeness check requires 17 distinct regular-season games per team;
duplicate provider rows do not count toward that minimum.

Schedule-provided rank prefixes (such as `#4 Texas`) are stripped before
opponent identity, UID hashing, and poll lookup. Displayed ranks come only from
the selected AP/CFP enrichment, so changing a schedule prefix cannot create a
new event or duplicate a rank in its title.

The September 2026 Texas event returns to its original UID
`football-calendar-college-football-osu-34c055f97199cd67@local` and continues that
UID's stored sequence. If the state contains the mistakenly published UID
`football-calendar-college-football-osu-45b8ca68d0259b49@local`, both the Ohio
State and combined feeds also publish a `STATUS:CANCELLED` event under that UID
with an incremented sequence. This cancellation is retained while the canonical
game remains in the schedule; later refreshes keep its sequence and timestamp
unchanged. Fresh state does not create the duplicate. Clients that ignore
cancellation entries may need their subscription removed and added again to
clear a cached duplicate. Existing state and published feeds are repaired by
the next normal refresh; this change does not redefine historical-ranking policy.

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

Changes to `site/` pushed to `main` also deploy automatically, allowing verified
feed corrections to publish immediately without waiting for the weekly refresh.

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
fixture_dir=$(mktemp -d)
python3 scripts/calendar_feed.py --fixture tests/fixtures/sample.json \
  --output "$fixture_dir" --state "$fixture_dir/feed-state.json"
```

Fixture health output is labeled `fixture`. Without `--state`, fixture runs store
state inside the output directory; explicit paths to the live state are rejected.
The checked-in fixtures are only for tests and are never published.
`osu-2026.html` contains simplified official schedule rows captured November 3,
2026; `osu-published-state.json` preserves the October 28 Ohio State revisions
with the migration metadata. Tests use these frozen snapshots so scheduled feed
updates do not change the regression baseline.

A normal refresh uses live sources:

```sh
python3 scripts/calendar_feed.py --output site
```

## Ranking policy

Before the first published CFP ranking, the feed seeks a poll explicitly named
`AP Top 25`. On/after the CFP release window it seeks an explicitly named CFP
poll. If that exact poll is unavailable, the rank is omitted rather than silently
substituting the Coaches poll. The `ranking_provider` adapter is intentionally
small so it can be replaced with a licensed/official structured source if needed.
