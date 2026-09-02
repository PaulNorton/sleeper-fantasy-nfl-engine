# fantasy-draft

Draft assistant for Sleeper fantasy football leagues. A CLI that reads a live
draft from the Sleeper API and recommends the next pick.

## Working agreement

**Commit after each change.** Do not batch work into one large commit and do not
wait to be asked. Once a change is made and its checks pass, commit it with a
message describing what changed and why. Work directly on `master`; this is a
single-author repository.

## Checks

```bash
PYTHONPATH=tests .venv/bin/python tests/test_engine.py
```

Runs the engine against live projections and synthetic drafts. It hits the
network (Sleeper and FantasyPros, both cached) and takes a few minutes. Every
check must pass before committing.

## What matters here

The analytics are the product. The UI is deliberately plain. When choosing
between a clearer number and a nicer display, pick the number.

Three files hold judgement calls rather than derived facts. Changing them
changes every recommendation, so they are commented and grouped:

- `outcomes.py` - spread constants behind every floor and ceiling. These are
  the least certain numbers in the project; they are not fitted to data.
- `valuation.py` - bench factors, injury discounts, the hindsight blend.
- `simulate.py` - how far actual draft position spreads around ADP.

## Constraints worth remembering

- **FantasyPros:** only the cheatsheet pages are read. Their robots.txt
  disallows `/ajax/`, `/api/`, `/json/`, and the full projections tables load
  from `/ajax/`. Do not scrape those. Identify the client honestly, honour the
  5-second crawl delay, cache responses.
- **Sleeper's `/league/{id}/drafts` returns abbreviated draft objects** with no
  `slot_to_roster_id`. Always re-fetch the chosen draft from `/draft/{id}`.
- **Sleeper user lookup takes a username handle, not an email**, and returns
  `200 null` rather than a 404 for a miss.
- **Monte Carlo noise is the usual cause of a surprising ranking.** Simulating
  seasons on top of draft order roughly doubled the variance of a score. Check
  stability across seeds before concluding the model is wrong.
- **Editing a projection invalidates the risk fields.** Call `attach_risk`
  after changing `points`, or the simulated season describes a different player.
