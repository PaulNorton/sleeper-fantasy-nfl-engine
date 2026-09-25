# sleeper-fantasy-nfl-engine

Draft assistant for Sleeper fantasy football leagues. A CLI that reads a live
draft from the Sleeper API and recommends the next pick, then carries on
in-season: setting your lineup, ranking waivers and drops, and flagging
players whose projection moved.

## Working agreement

**Commit after each change.** Do not batch work into one large commit and do not
wait to be asked. Once a change is made and its checks pass, commit it with a
message describing what changed and why. Work directly on `master`; this is a
single-author repository.

## Checks

```bash
PYTHONPATH=tests .venv/bin/python tests/test_engine.py
PYTHONPATH=tests .venv/bin/python tests/test_season.py
```

`test_engine.py` runs the draft-time engine against live projections and
synthetic drafts. `test_season.py` covers the in-season tools - lineup,
waivers, drops, alerts - the same way. Both hit the network (Sleeper and
FantasyPros, both cached) and take a few minutes. Every check must pass
before committing.

## What matters here

The analytics are the product. The UI is deliberately plain. When choosing
between a clearer number and a nicer display, pick the number.

Three files hold judgement calls rather than derived facts. Changing them
changes every recommendation, so they are commented and grouped:

- `outcomes.py` - spread constants behind every floor and ceiling. These are
  the least certain numbers in the project; they are not fitted to data.
- `valuation.py` - bench factors, injury discounts, the hindsight blend.
- `simulate.py` - how far actual draft position spreads around ADP.
- `changes.py` - how far a projection has to move before `alerts` flags it.
- `waivers.py` - what a move has to be worth before it is worth making, and
  the injury and drift model that prices bench depth: how often players get
  hurt, for how long, how much of the work a teammate inherits, and how far a
  projection wanders.
- `projections.py` - the three consensus weights (draft, weekly, rest of
  season). How much of every ordering comes from the experts rather than
  from Sleeper.

## Constraints worth remembering

- **FantasyPros:** only the ranking pages under `/nfl/rankings/` are read -
  the season cheatsheet, the weekly rankings, and the rest-of-season list.
  Their robots.txt disallows `/ajax/`, `/api/`, `/json/`, and the full
  projections tables load from `/ajax/`. Do not scrape those. Identify the
  client honestly, honour the 5-second crawl delay, cache responses.
- **`qb.php` serves an empty ranking to a plain HTTP client** - literally
  `var ecrData = []` - and fills its table in from a script. Weekly
  quarterbacks come off `superflex.php` filtered to QB, which renders the same
  ranking server-side. Check for an empty list, not a non-200, when a
  FantasyPros page seems to have lost its data.
- **FantasyPros ranks the upcoming week only.** `?week=N` for a later week
  returns an empty list rather than an error. Weeks past the next one use the
  rest-of-season ranking instead.
- **Blend the consensus before applying an injury discount.** Blending
  re-seats a position's points curve, so it moves points between players.
  Discount first and a player ruled out hands his near-zero to whoever the
  blend happens to seat last.
- **Sleeper's `/league/{id}/drafts` returns abbreviated draft objects** with no
  `slot_to_roster_id`. Always re-fetch the chosen draft from `/draft/{id}`.
- **Sleeper serves `/draft/{id}` and `/draft/{id}/picks` through Cloudflare**
  with `stale-while-revalidate`, so a plain request just after a pick returns
  the copy from before it. `SleeperClient._live` adds a unique query parameter
  to reach the origin; a client `Cache-Control: no-cache` is ignored. Use it for
  anything that must reflect the last pick, and nothing else.
- **Sleeper user lookup takes a username handle, not an email**, and returns
  `200 null` rather than a 404 for a miss.
- **Monte Carlo noise is the usual cause of a surprising ranking.** Simulating
  seasons on top of draft order roughly doubled the variance of a score. Check
  stability across seeds before concluding the model is wrong.
- **Editing a projection invalidates the risk fields.** Call `attach_risk`
  after changing `points`, or the simulated season describes a different player.
- **A player on bye has no weekly stat line at all**, so he drops out of
  `load_week_points` for that week rather than coming back as a zero. The
  rest-of-season model in `waivers.py` relies on this: byes need no special
  case because an absent player simply cannot be seated.
- **In-season value is rest-of-season value.** A season-long projection counts
  games already played, so it is the wrong number for any decision taken after
  week 1. Sum the remaining weeks instead.
