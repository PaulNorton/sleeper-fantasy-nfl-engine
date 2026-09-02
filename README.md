# fantasy-draft

A draft assistant for Sleeper leagues. It reads your league and live draft from
the Sleeper API and tells you which player to take next.

## The question it answers

Not "who is the best player left" — "which pick raises my expected final
starting lineup by the most points." Those give different answers. The best
player left is often a quarterback you do not need.

## Setup

```bash
uv venv && uv pip install -e .
.venv/bin/fdraft setup --username YOUR_SLEEPER_NAME
```

Use your Sleeper **username handle**, not the email you log in with — the public
API does not accept email addresses.

`setup` saves your league to `fdraft.json`, so later commands need no flags. If
you are in several leagues it lists them and asks you to pass `--league-id`.
You can also set up from a league id alone, taken from your league URL:

```bash
fdraft setup --league-id YOUR_LEAGUE_ID --slot 7
```

Every command takes `--league-id` to work against your other league without
changing the saved default.

## Use

```bash
fdraft watch          # follow the live draft, re-rank after every pick
fdraft board          # rank the board once
fdraft available -p RB   # raw value list, no roster logic
fdraft roster         # your picks and which starter slots are still open
```

Mock drafts work too: `fdraft board --draft-id 123456789 --slot 4`.

## How a pick is scored

Every number traces back to one quantity:

```
utility(roster) = points of your best legal starting lineup
                + insurance value of your bench
```

A pick is worth the increase in that number, measured over the whole rest of
the draft.

**1. Projections in your scoring.** Sleeper publishes a projected stat line per
player. The app applies your league's own `scoring_settings` to those stats, so
a tight-end premium, a 6-point passing touchdown or a first-down bonus changes
the projections rather than being ignored. Injury designations discount the
projection.

**1b. Expert consensus on top.** FantasyPros aggregates 100+ analysts. Their
cheatsheet ordering is blended 50/50 with Sleeper's, as a *ranking* rather than
as points: the league-scored points curve is kept and players are re-seated on
it. That adopts the crowd's ordering while keeping a realistic spread of points.
The same source supplies bye weeks and, more importantly, how much the experts
disagree - which drives the risk model below. Turn it off with
`--no-consensus`.

**2. Replacement level from your roster shape.** A position's replacement level
is the projection of the last player who would start league-wide, counting flex
demand. In a 12-team league with two running back slots and a flex, that is
roughly the 29th running back. Value over replacement is the raw currency.

**3. Your lineup, not a generic one.** Adding a player is worth what he adds to
your optimal starting lineup. The third quarterback in a one-QB league adds
nothing to the lineup and little to the bench, so he scores near zero no matter
how high he sits on a ranking list. Superflex changes this automatically,
because the lineup has a slot he can fill.

**4. What the rest of the draft does.** The app simulates the remaining draft
many times. Opponents are modelled as ADP drafters: each remaining player gets a
sampled landing spot from a normal centred on his ADP, truncated to "has not
gone yet", and opponents take players in that order. At each of your own future
picks the simulation takes whichever player adds the most utility. A candidate's
score is your average final roster utility across those simulations, so it
already nets out everything you would have taken instead.

This is what stops the obvious failure modes. Taking a run on a thin position
early is rewarded because the simulation shows the drop-off. Taking a kicker in
round 8 is punished because the simulation shows you can still get one in round
15. Stacking a fourth running back is punished because he never starts.

## Ranges of outcomes

A projection is a mean, and two players with the same mean are not the same
asset. Uncertainty is modelled in **rank** space, not points:

```
finish = (projected_rank + 8) * exp(spread * Z) - 8
```

and the points are read off the position's value curve. Modelling it this way
rather than as a spread in points gets three things right:

- **Skew flips with quality.** The value curve is steep at the top and flat at
  the bottom. The projected RB1 has little room above him and a long way to
  fall, so his range leans down. A projected RB40 has a bounded floor and a
  long way to climb, so his range leans up. That asymmetry - low floor, high
  ceiling - is not hand-coded anywhere; it falls out of the curve's shape.
- **Ceilings stay believable.** Upside is capped near the top of the curve
  instead of running off to totals nobody has scored.
- **Rank moves multiplicatively.** Slipping ten places wrecks the RB2 and does
  nothing to the RB50.

Spread comes from two independent sources, added in quadrature: irreducible
season variance by position (quarterbacks hold their rank best, kickers barely
at all), widened for rookies and injury designations; and expert disagreement,
taken from the standard deviation of FantasyPros ranks. Measured across the
player pool, rookies come out at 1.6x their median in range against 1.1x for
settled veterans.

### How upside is priced

Not with a bonus. The simulation draws a realised season for every player, and
the lineup optimiser then seats the roster on those realised numbers. A
boom/bust bench player who breaks out is *started*; when he busts he is
*benched*. That option value appears on its own, and it is worth more for a
bench flier than for the only quarterback you own, exactly as it should be.

Seating a lineup on realised season totals assumes perfect hindsight, which
flatters upside - you do not know in week 1 who the breakout is. Seating by
preseason projection assumes you never adjust. Real managers learn within a few
weeks, so the two are blended 70/30 (`HINDSIGHT` in `valuation.py`).

### What that turns out to be worth

Less than intuition suggests, and the reason is worth knowing. A roster is
fifteen players, so its total is far steadier than any one player: individual
upside diversifies away. Asking for a floor or a ceiling with `--risk 0.25` or
`--risk 0.75` barely reorders the board.

Variance pays when you have to **beat nine other people**. Finishing with the
most points is a tail event, and a steady roster that projects third gets you
third. `--objective win` simulates the other nine rosters on the same seasons
and maximises your share of the title odds instead of your points. It is convex
in your own total, so upside that occasionally wins outright counts for more
than the same points delivered reliably. Even then the reordering is modest -
a real effect, not a large one.

## Reading the table

| Column | Meaning |
|---|---|
| Proj | Season points in your scoring |
| VOR | Points above replacement at his position |
| Floor / Ceil | His 10th and 90th percentile season |
| Wait | Points lost at his position if you pass and wait a turn |
| Back | Chance he is still there at your following pick |
| Tier | His tier at the position, and how many players are left in it |
| Cost | Expected points behind the top recommendation |

The top row is the pick. `Back` is the column that tells you whether you can
afford to take someone else first.

## Using your own projections

Projections drive everything, and Sleeper's are only one opinion. Overlay your
own:

```bash
fdraft board --projections my_ranks.csv
```

The CSV needs a name column and any of `points`, `adp`. Rows are matched on
name, and on position when the column is present.

```csv
player,pos,points,adp
Ja'Marr Chase,WR,341.2,2.1
Bijan Robinson,RB,318.0,3.4
```

## What it does not model

- **Bye-week clashes.** Bye weeks are read from FantasyPros and shown, but two
  starters sharing a bye are not yet penalised.
- **Correlation between players.** Two receivers on one team compete for the
  same targets; the simulation draws their seasons independently.
- **Opponent roster needs.** Opponents follow ADP; they do not skip a position
  because they already filled it. ADP already prices this in on average.
- **Keepers and auction drafts.** Snake and linear drafts only.
- **Trades during the draft**, beyond pick trades already recorded by Sleeper.

## Tuning

`--sims` controls simulation count. The default of 300 ranks the board in about
two seconds. Simulating seasons on top of draft order roughly doubled the noise
in a score, so the estimator uses common random numbers and antithetic pairs;
below about 200 sims the ordering starts to wobble.

The judgement calls live in three files and are meant to be edited:
`valuation.py` holds bench factors, injury discounts and the hindsight blend;
`simulate.py` holds how far ADP spreads around its mean; `outcomes.py` holds the
spread constants behind every floor and ceiling. Those spread constants are
judgement, not fitted to data - they are the least certain numbers in the
project.

## Data sources

- **Sleeper** for the league, the live draft, projected stat lines and ADP. No
  authentication needed.
- **FantasyPros** for expert consensus rank, expert disagreement and bye weeks,
  read from the public cheatsheet pages. Those pages are not under any path
  disallowed by their robots.txt, requests identify themselves honestly and the
  published 5-second crawl delay is honoured, with responses cached for six
  hours. The full projections tables are deliberately *not* taken: they load
  from `/ajax/`, which robots.txt disallows.

## Checks

```bash
PYTHONPATH=tests .venv/bin/python tests/test_engine.py
```

Runs the engine against live projections and a synthetic draft, asserting that
it stops drafting quarterbacks once the slot is filled, raises them in
superflex, fills mandatory slots before the draft ends, rotates off a saturated
position, and treats a fallen player as likely to go soon.
