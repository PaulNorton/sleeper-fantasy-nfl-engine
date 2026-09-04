# fantasy-draft

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

## Constraints worth remembering

- **FantasyPros:** only the cheatsheet pages are read. Their robots.txt
  disallows `/ajax/`, `/api/`, `/json/`, and the full projections tables load
  from `/ajax/`. Do not scrape those. Identify the client honestly, honour the
  5-second crawl delay, cache responses.
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
