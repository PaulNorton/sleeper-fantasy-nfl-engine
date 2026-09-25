# sleeper-fantasy-nfl-engine

A draft assistant for Sleeper leagues. It reads your league and live draft from
the Sleeper API and tells you which player to take next.

## The question it answers

Not "who is the best player left" — "which pick raises my expected final
starting lineup by the most points." Those give different answers. The best
player left is often a quarterback you do not need.

## Setup

```bash
uv venv && uv pip install -e .
.venv/bin/sleeper-fantasy-nfl-engine setup --username YOUR_SLEEPER_NAME
```

Use your Sleeper **username handle**, not the email you log in with — the public
API does not accept email addresses.

`setup` saves your league to `sleeper-fantasy-nfl-engine.json`, so later commands need no flags. If
you are in several leagues it lists them and asks you to pass `--league-id`.
You can also set up from a league id alone, taken from your league URL:

```bash
sleeper-fantasy-nfl-engine setup --league-id YOUR_LEAGUE_ID --slot 7
```

Every command takes `--league-id` to work against your other league without
changing the saved default.

## Commands

Every command reads `sleeper-fantasy-nfl-engine.json` for defaults, so after `setup` the flags are
optional. Any flag overrides the saved value for that one run.

### Shared flags

These appear on `board`, `watch`, `roster` and `available`.

| Flag | Default | What it does |
|---|---|---|
| `--username`, `-u` | from config | Your Sleeper handle. Identifies which team is yours. Not an email. |
| `--league-id`, `-l` | from config | Sleeper league id, from your league page URL. Use it to work against a second league without changing the saved default. |
| `--draft-id`, `-d` | from config | Target a draft directly. Works for standalone mock drafts that belong to no league. |
| `--season` | current | Season, e.g. `2026`. |
| `--slot` | detected | Your draft slot, 1-based. Only needed when the draft order is not published yet and you are not in the league's roster list. |

`roster` and `available` also take `--projections` and `--no-consensus`, below,
so they read the same data the recommendations do.

### `sleeper-fantasy-nfl-engine setup`

Saves your league to `sleeper-fantasy-nfl-engine.json` so other commands need no flags.

| Flag | Default | What it does |
|---|---|---|
| `--username`, `-u` | — | Your Sleeper handle. Looks up your leagues for the season. |
| `--league-id`, `-l` | — | Skip the league picker. Required if you are in more than one league. |
| `--slot` | — | Save your draft slot. Worth setting when configuring from a league id alone. |
| `--season` | current | Season to search for leagues. |

Give a username, a league id, or both. With only a league id it saves the
league without resolving which team is yours, so add `--slot`.

```bash
sleeper-fantasy-nfl-engine setup --username YOUR_SLEEPER_NAME                             # lists your leagues
sleeper-fantasy-nfl-engine setup --username YOUR_SLEEPER_NAME --league-id YOUR_LEAGUE_ID  # picks one
sleeper-fantasy-nfl-engine setup --league-id YOUR_LEAGUE_ID --slot 7                      # no account needed
```

### `sleeper-fantasy-nfl-engine leagues`

Lists your leagues for a season with their ids, team counts and status. Takes
`--username` and `--season` only.

### `sleeper-fantasy-nfl-engine board`

Ranks the board for your next pick and exits. The main command.

| Flag | Default | What it does |
|---|---|---|
| `--limit`, `-n` | 12 | How many players to rank. |
| `--sims` | 300 | Draft simulations per candidate. Below ~200 the ordering starts to wobble; more is steadier and slower. 300 ranks in about two seconds. |
| `--strategy` | `lineup` | How to rank. `lineup` scores what a player adds to your starting lineup and backtests best. `vona` also subtracts what his position would still offer at your next pick, which measurably costs points. `simulate` plays the rest of the draft out — worst of the three; see [what the backtest found](#what-the-backtest-found). |
| `--objective` | `points` | `points` maximises your expected season total. `win` simulates the other rosters too and maximises your share of the title odds. See [what variance is worth](#what-that-turns-out-to-be-worth). |
| `--risk` | mean | Percentile of your simulated seasons to optimise, e.g. `0.25` for the floor, `0.75` for upside. In practice this rarely changes the top pick; `--objective win` is the knob that bites. |
| `--projections` | — | CSV of your own projections and/or ADP, overlaid on everything else. |
| `--no-consensus` | off | Skip FantasyPros. Falls back to Sleeper projections alone, and loses bye weeks and the expert-disagreement half of the risk model. |

### `sleeper-fantasy-nfl-engine watch`

The same board, refreshed as the draft happens. Takes every `board` flag plus:

| Flag | Default | What it does |
|---|---|---|
| `--interval`, `-i` | 10.0 | Seconds between polls. It only re-ranks when a pick is actually made. |

Every pick is ranked under all three strategies. The board is ordered by
`--strategy`, and two extra columns give what `vona` and `simulate` made of the
same player, so a candidate only one strategy likes is visible without leaving
the screen. A line above the table says whether they agree on the top pick.
Under `--objective win` only `simulate` runs, since the other two cannot measure
a share of the title odds.

The three do not cost the same. `lineup` and `vona` are closed-form and rank in
about 0.1s; `simulate` plays the rest of the draft out and takes seconds — 15s
on a full pre-draft board at `--sims 300`. So the board paints from the fast two
straight away and `simulate` runs on a worker thread behind it. Its column shows
`…` until the first result lands, and `Simu*` in yellow while it describes an
earlier pick. The agreement line ignores it while it is behind rather than
claiming a consensus from a stale ranking.

The line under the header answers "am I looking at the current board": how many
picks are in, when the draft was last read, how long ago the last new pick
landed, whether the read came straight from Sleeper, and how current the
simulate column is — `simulate current`, `simulate ranking 7s, column 1 pick
behind`, or `simulate failed: …`. It ticks once a second, not once a poll. A
failed poll leaves the last good board up and says so in red rather than
blanking the screen.

Safe to start before the draft opens: it re-reads the pick order every poll, so
it picks up a randomised order at kickoff.

If `simulate` lags more than a pick behind, drop `--sims`; it affects that
strategy only, and the rest of the board is unaffected by the wait.

### `sleeper-fantasy-nfl-engine compare`

Ranks the board under all three strategies (`lineup`, `vona`, `simulate`) side
by side, for a second opinion at the moment of your actual pick.

`lineup` backtests clearly best over 2024-25 - see
[what the backtest found](#what-the-backtest-found) - and is what `board` and
`watch` act on. This command doesn't change that; it's for checking whether the
strategies agree. Agreement across all three is a confidence signal. A split is
worth a second look before you lock in `lineup`'s pick.

| Flag | Default | What it does |
|---|---|---|
| `--limit`, `-n` | 8 | How many picks to show per strategy. |
| `--sims` | 300 | Simulations for the `simulate` column only; the other two are closed form. |

Takes the same `--projections` and `--no-consensus` flags as `board`.

### `sleeper-fantasy-nfl-engine roster`

Your drafted players seated into starter slots, and which slots are still open.

### `sleeper-fantasy-nfl-engine available`

The best players still on the board by raw value, with floor, ceiling, ADP and
bye. No roster logic, no simulation.

| Flag | Default | What it does |
|---|---|---|
| `--pos`, `-p` | all | Filter to one position, e.g. `RB`. |
| `--limit`, `-n` | 25 | How many players to list. |

### In-season commands

Once the draft is over, your roster comes from the league's live roster list
instead of the draft board, and it moves week to week through waivers, drops
and trades. These four commands cover that. They share `--username`,
`--league-id`, `--season`, `--projections` and `--no-consensus` with the
draft commands, plus `--roster-id` in place of `--slot` if your team cannot be
detected automatically.

#### `sleeper-fantasy-nfl-engine lineup`

Compares the lineup Sleeper has saved to the one your week's projections say
is best, and lists only the swaps where they disagree.

| Flag | Default | What it does |
|---|---|---|
| `--week` | current | Which week to plan for. |

The ranking is Sleeper's weekly projection blended 50/50 with FantasyPros'
weekly expert consensus, the same way the draft board blends the cheatsheet:
as a *ranking*, so each position is re-seated on its own points curve. Sleeper
publishes one projection; the consensus is 70-odd analysts who have read the
practice report. Pass `--no-consensus` to rank on Sleeper alone.

FantasyPros publishes weekly rankings for the upcoming week only, so
`--week` set to a later one falls back to Sleeper and says so, rather than
passing last week's opinion off as this week's.

Each row carries the fixture, the blended positional rank, and how far apart
the experts are about it (`WR14 ±3`). The **Call** column reads `coin flip`
when the gap between the two players is smaller than that disagreement —
a swap that is not worth making, however the projections happen to order them.

A player ruled `Out` for the week scores close to zero rather than the milder
season-long injury discount `board` uses — missing this week's game is close
to certain, not a haircut on a whole season. The discount is applied *after*
the consensus blend, since re-seating moves points between players.

#### `sleeper-fantasy-nfl-engine waivers`

Answers one question: should you sign anybody today, and who do you drop for
him. It recommends a signing only when both of these hold:

1. **It raises your rest-of-season total**, after paying for the drop. The
   drop's price includes the depth he gives you: the weeks he covers a bye,
   an injury, or a starter who loses his job.
2. **Today is the time to make it.** A later date can be worth more, and the
   wire may still hold someone like him then.

| Flag | Default | What it does |
|---|---|---|
| `--pos`, `-p` | all | Only consider one position. |
| `--weeks` | rest of season | How many weeks ahead to value. `--weeks 3` asks "just get me through the next three". |
| `--sims` | 1000 | Simulated seasons behind every value. `0` takes projections at face value, with no injuries. |
| `--limit`, `-n` | 8 | How many moves to list. |

**The yardstick is rest-of-season points, seated week by week.** Your roster
is made into a starting lineup separately for every week left, and the weeks
are summed. Byes need no special case: a player on bye has no stat line that
week, so the lineup seats whoever is left. Games already played do not count.
An empty starter slot scores zero, not replacement level. The draft engine
floors every slot at the waiver wire because it assumes you will stream
someone; that assumption is what is being decided here.

**Depth is priced by simulation.** Taken at face value, projections say every
starter plays every week at today's number, and a backup who never cracks the
lineup is free to drop. So the weeks after this one are played out 1000 times:

- Players get hurt. A running back misses about 16% of weeks, a receiver or
  tight end 12%, a quarterback 9%, each absence about three weeks long.
- Part of an injured player's projection passes to his healthy teammates at the
  same position. This is what a handcuff is worth.
- Every projection drifts. After nine weeks a receiver's number is typically a
  third higher or lower than today's: a job won or lost.

Each week's lineup is seated on what you would know by then. A backup is worth
what he scores in the runs where he is needed, and that is the cost of dropping
him. Every roster meets the same injuries in the same runs, so the difference
between two rosters is far steadier than either total. The constants live in
`waivers.py`; they are judgement, not fitted.

**A move has a date.** Signing in week k changes only weeks k onward, so each
possible date has its own value. When the player you would drop still earns
early weeks, a later date is worth more. Whether to wait depends on the wire:

- If other free agents at his position are within 3 points of him, he is
  replaceable. Sign him only if now is his best date; otherwise wait.
- If nobody on the wire is close, sign him now, as long as signing now
  already clears 3 points. Waiting is how you lose him.

A tie between dates goes to the later one, so no roster spot is held early for
nothing.

The table shows the arithmetic behind each move. **Adds** is what he scores for
you if nobody had to go. **Drop costs** is what the dropped player would have
scored for you. **Net** is the difference, signing today. **Starts** is how
many weeks he would start, on average. **When** is the verdict: `sign now`,
or `wait until week N` with what the move is worth then.

A player on IR or taxi is never offered as the drop. Releasing him frees his
IR or taxi slot, not the active spot a signing needs.

Every week carries expert consensus, not Sleeper alone. The imminent week gets
the weekly ranking at the same 50/50 weight `lineup` uses. Later weeks get the
**rest-of-season** ranking at 0.35, because FantasyPros ranks only the next
week ahead. The lower weight is deliberate: a rest-of-season rank is
cumulative, so a player who misses three games already ranks below an equal
player who does not, and the week-by-week seating scores those missed weeks a
second time by leaving him out of them.

#### `sleeper-fantasy-nfl-engine alerts`

Snapshots every player's season projection and, on the next run, flags anyone
whose number moved more than the threshold in `changes.py` since the last one.
There is no news feed behind it — an injury and a quietly lost starting job
both show up the same way, as a projection that jumped. Silent on the first
run for a league, since there is nothing yet to compare against.

```bash
sleeper-fantasy-nfl-engine lineup             # this week's start/sit changes
sleeper-fantasy-nfl-engine waivers            # sign anyone? drop whom?
sleeper-fantasy-nfl-engine waivers --weeks 3  # value only the next three weeks
sleeper-fantasy-nfl-engine waivers -p RB      # only consider running backs
sleeper-fantasy-nfl-engine alerts             # who moved since last time
```

### `sleeper-fantasy-nfl-engine backtest`

Replays a finished season to see how the engine would have drafted. Teams draft
knowing only that season's preseason projections and ADP; rosters are then
scored on what actually happened. Every strategy faces identical drafts, so only
the focal seat's choices differ.

| Flag | Default | What it does |
|---|---|---|
| `--season` | 2025 | A finished season to replay. |
| `--reps` | 3 | Repetitions per draft slot. More reps, less noise. |
| `--slots` | all | Comma-separated draft slots to test, e.g. `1,5,10`. |
| `--sims` | 120 | Simulations per pick while drafting. Lower than live use, since a backtest makes thousands of picks. |
| `--rounds` | 15 | Rounds per draft. |
| `--opponent` | `adp` | What the other nine seats do: `adp`, `vor` or `need`. |

Scoring rules come from your saved league, so a backtest measures the engine in
*your* format. It needs `--league-id` if you have not run `setup`.

```bash
sleeper-fantasy-nfl-engine watch                                   # follow the draft live
sleeper-fantasy-nfl-engine board --objective win -n 20             # rank by title odds
sleeper-fantasy-nfl-engine available -p RB -n 30                   # who is left at running back
sleeper-fantasy-nfl-engine board --league-id YOUR_OTHER_LEAGUE_ID  # your other league
sleeper-fantasy-nfl-engine compare                                 # do the strategies agree on your pick?
sleeper-fantasy-nfl-engine backtest --season 2025                  # how would it have drafted last year
```

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

**4. Your floor is the waiver wire, not zero.** Every lineup slot is floored at
replacement level, whether you drafted someone for it or not. A slot you skipped
is worth what streaming gives you; a starter who collapses is not worth his 40
points, because you drop him in October. Flooring the downside is most of what
makes upside worth paying for — a boom-or-bust player's bad seasons cost only
the gap down to waivers, while his good ones are yours in full. This single
change was worth more than anything else in the model.

**5. Bye weeks.** Two starters sharing a bye means one week of starting a waiver
body in a slot that should have held a starter. The cost is worked out exactly:
the lineup is seated again for each bye week your roster has, because whether a
clash hurts depends on whether your bench happens to cover that position. Byes
come from the NFL schedule, as the week each team does not play.

**6. A running back bonus in the first four rounds.** Conventional wisdom says
prioritise running backs early, on the theory that touch-share concentration
and in-season attrition make a bell-cow scarcer than his points imply. Tested
rather than assumed - see `tests/hypotheses.py` - and confirmed: +3.1 ± 1.1
points per draft over 280 paired backtested drafts (t = 2.79). Off in
superflex, where it was never tested and quarterbacks already dominate the
early rounds for an unrelated reason.

Optionally the app can also subtract what a position would still offer at your
next pick (`--strategy vona`), or simulate the remaining draft thousands of
times (`--strategy simulate`). Both are more sophisticated and both measurably
lose points; see below.

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

## What the backtest found

`sleeper-fantasy-nfl-engine backtest` replays a finished season: draft on preseason projections
only, score the roster on what actually happened. Two seasons, ten draft slots,
six repetitions, every strategy facing identical drafts so the comparison is
paired.

| strategy | 2025 | 2024 | vs ADP |
|---|---|---|---|
| `lineup` (shipped) | 2351 | 2454 | **+243** |
| best value over replacement | 2258 | 2475 | +207 |
| fill starters first | 2259 | 2457 | +199 |
| `vona` | 2234 | 2488 | +202 |
| ADP | 2133 | 2186 | 0 |

Pooled over 120 paired drafts:

| comparison | mean diff | std err | t |
|---|---|---|---|
| `lineup` − ADP | +243 | 15 | 16.22 |
| `lineup` − fill starters | +44 | 9 | 4.99 |
| `lineup` − `vona` | +42 | 12 | 3.56 |
| `lineup` − value over replacement | +36 | 11 | 3.41 |
| bye-aware − bye-blind | +10.4 | 3.5 | 2.98 |
| Robust RB bonus − without it | +3.1 | 1.1 | 2.79 |

Three results are worth stating plainly.

**Simulating the whole draft is the worst option tested.** In 2025 it finished
below ADP. Greedy lineup-marginal separates the best candidate from the second
by about 75 points, but the rollout turns that same difference into 4.5 points
of final roster value, because simulated future-you simply drafts a comparable
player instead. Seed-to-seed noise at 300 simulations is 3.3 points, so what
survives the compression barely clears the noise floor and the top two
candidates swap depending on the seed.

It is kept because it sees one thing the closed-form strategies miss. Given two
identical players, one who will last and one who will not, taking the scarce one
lets you end up with *both*. The closed-form strategies compare each candidate
against the position's best player next turn, which is the same number either
way, so they rate them equally. The rollout separates them by 41 points.

**Subtracting the wait value costs points.** `vona` implements exactly the "can
I get him later" reasoning, in closed form and with no noise, and it is worse
than ignoring the question entirely. The wait columns are still shown because
they are useful to read; the engine no longer acts on them.

**Five of six tested strategy hypotheses did not survive contact with data.**
Alongside Robust RB (kept, above): reach-aversion - discounting a pick by how
far it reaches past his ADP, to hedge against the model being wrong about one
player - lost by 7.3 ± 3.6 points (t = -2.04), since the model's edge over ADP
is the point of a value model and pulling picks back toward consensus gives
some of it away. Ceiling-chasing once a roster's starters are filled showed no
signal (+2.9 ± 2.5, t = 1.13). A tier-cliff bonus - reward taking the last
player before a real gap to the next echelon - also showed nothing (+2.7 ± 4.6,
t = 0.60).

The more useful negative result: applying Robust RB's exact bonus to WR, TE and
QB instead of RB tests whether it is a genuine RB effect or an artifact of
rewarding any early pick. It is not generic - WR lost clearly (-9.0 ± 2.6,
t = -3.42), TE lost suggestively (-7.9 ± 3.7, t = -2.13), QB showed nothing
(-2.2 ± 4.2). That two of three lose money is stronger evidence for Robust RB
than a clean win alone would have been. `tests/hypotheses.py` reproduces all nine comparisons.

Three more were tested afterward, all rejected. A parameter sweep around
Robust RB's setting (rounds in 2-6, bonus in 0.08-0.30) found nothing better
than the shipped (4, 15%); the best alternative reached t = 1.49, short of
significance. Handcuffing - bonusing a backup running back who shares a team
with a starter already on the roster - lost clearly (-2.8 ± 1.1, t = -2.53
over 280 paired drafts): a handcuff is a bench slot that pays off in only the
one season a starter is actually hurt, and the waiver floor already prices
that without a manual override. Draft-run detection - boosting a position
being taken faster than its fair share in the actual draft underway, tested
against `--opponent need` since ADP bots create no genuine runs to detect -
showed no signal (-4.2 ± 3.0, t = -1.43) even with opponents capable of
producing one.

By that point every decision-time heuristic layered on top of marginal value
had lost except Robust RB, which won for one position only - suggesting it
compensates for something the input calibration under-represents for RB
specifically, not that "prioritise a position" is a good heuristic in general.
Three more hypotheses tested that directly: RB-specific waiver depth (no
configuration beat the shared 0.5-round default), RB-specific bench factor
(raising it was clearly worse; lowering it to zero looked promising at 280
paired drafts, t = 2.15, but regressed to t = 1.51 at 600 - a false positive
settling toward zero under scrutiny), and uncapping RB's shrinkage from 1.00
toward its measured 1.11 (flat at best, worse elsewhere). None replicated
Robust RB's effect. It stands as a genuine, position-specific exception, not a
proxy for a calibration constant that could substitute for it.

**You do not have to fill every role - for the defence.** A slot left empty can
be covered off waivers all season, so the pick it costs might be better spent
elsewhere. The verdict splits by how much a position's projections are worth.
Punting a slot the projections understand is expensive: over 240 paired drafts,
punting TE lost 30.2 ± 5.2 (t = -5.83) and punting QB lost 25.0 ± 3.5
(t = -7.08). Skipping the defence gained 3.6 ± 0.8 (t = 4.39). Skipping the
kicker was neutral (+2.8 ± 1.8, t = 1.56).

**That result needed a control, and the first version of the test lacked one.**
A strategy that skips the defence frees its round-14 pick and takes its kicker a
round early, drawing from a pool of about thirty kickers whose realised seasons
swing 40 points and whose projections predict nothing. The two arms drafted
kickers projected 72.1 and 72.8 whose actual seasons differed by 41 points - an
artifact larger than the effect being measured, and enough to reverse its sign.
Holding the kicker to the final round in both arms, verified to draft the same
kicker in 10 of 10 seats, is what produces the +3.6. Any experiment that shifts
behaviour in rounds 14-15 is exposed to this; a sweep of `SHRINKAGE["DEF"]` was
contaminated the same way.

The cause is not that the engine believes defence projections. At a shrinkage of
zero, where every defence is worth exactly replacement level, it still drafts
one: by the final picks every candidate has a marginal value near zero and the
ranking breaks the tie by board order. With the roster held fixed the defence
pick is worth 1.4 blended points and the last skill pick 8.4. The model is
indifferent where the season is not. Not yet shipped - the fix is a tie-break
among near-zero candidates rather than a constant, and the gain rests on scoring
an unfilled defence slot at the 10th-best defence's actual season.

Two seasons is a thin basis and the season-to-season swing is large - `lineup`
beat `vona` by 117 points in 2025 and lost to it by 34 in 2024.

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
sleeper-fantasy-nfl-engine board --projections my_ranks.csv
```

The CSV needs a name column and any of `points`, `adp`. Rows are matched on
name, and on position when the column is present.

```csv
player,pos,points,adp
Ja'Marr Chase,WR,341.2,2.1
Bijan Robinson,RB,318.0,3.4
```

## What it does not model

- **In-season additions.** Downside is floored at the waiver wire, but the
  upside of picking up a breakout is not modelled. Real rosters gain more from
  waivers than this assumes, which means late-round picks are worth even less
  than the model already says.
- **Injuries that have not happened yet.** The rest-of-season horizon behind
  `waivers` takes every remaining week's projection at face value, so it sees
  no reason to hold a backup as insurance. It will happily suggest dropping
  your third running back, because in a season where nobody gets hurt that
  spot is genuinely idle.
- **Who else wants the same free agent.** There is no ownership or bid data
  here, so `waivers` cannot tell you what a claim will cost in FAAB, only
  what the player is worth to you.
- **Correlation between players.** Two receivers on one team compete for the
  same targets; their seasons are drawn independently.
- **Opponent roster needs.** Opponents follow ADP; they do not skip a position
  because they already filled it. ADP already prices this in on average.
- **Superflex.** The backtest replays 1QB leagues, so nothing about superflex
  valuation is validated.
- **Keepers and auction drafts.** Snake and linear drafts only.
- **Trades during the draft**, beyond pick trades already recorded by Sleeper.

## Tuning

`--sims` only applies to `--strategy simulate` and `--objective win`. The default
of 300 ranks the board in about two seconds; below about 200 the ordering
wobbles between seeds. The default `vona` strategy runs in closed form and is
effectively instant.

The judgement calls live in five files and are meant to be edited:
`valuation.py` holds bench factors, injury discounts (season-long and
weekly) and the hindsight blend; `simulate.py` holds how far ADP spreads
around its mean; `outcomes.py` holds the spread constants behind every floor
and ceiling; `changes.py` holds how far a projection has to move before
`alerts` flags it; `waivers.py` holds how much a move has to be worth before
it is worth making, and how few weeks a player can start in before he counts
as a streamer rather than an upgrade. The `outcomes.py` constants are
judgement, not fitted to data - they are the least certain numbers in the
project.

## Data sources

- **Sleeper** for the league, the live draft, live rosters, projected stat
  lines (season-long and per week) and ADP. No authentication needed. The
  draft and its picks are fetched past Sleeper's CDN
  with a unique query parameter. Those two endpoints are served `public,
  s-maxage=..., stale-while-revalidate=300`, so a plain request in the seconds
  after a pick gets the copy from before it — the board appears to flick between
  behind and current. Cloudflare ignores a client `Cache-Control: no-cache`
  there but keys on the full URL, so the parameter is what reaches the origin.
  Everything else — league, rosters, users, projections — still takes the cached
  copy, on-disk here as well as at the edge.
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
position, treats a fallen player as likely to go soon, prices rookies wider than
veterans, and records which strategies separate a scarce player from an
identical one who will last.

```bash
PYTHONPATH=tests .venv/bin/python tests/test_season.py
```

The same, for the in-season tools: weekly projections come back game-status
discounted, the horizon ignores weeks already played, a bye-week hole is
scored honestly and a fill-in credited for plugging it, a one-week fill-in is
deferred to the week it is needed while a week-in-week-out upgrade is not, an
add and the drop paying for it are netted as one decision, and a snapshot
flags a real swing while ignoring normal week-to-week noise.

```bash
PYTHONPATH=tests .venv/bin/python tests/hypotheses.py
```

Reproduces the strategy-improvement hypotheses tested against the shipped
engine: one kept (Robust RB), the rest rejected - reach-aversion,
ceiling-chasing, tier cliffs, handcuffing, draft-run detection, and leaving a
starter slot empty to stream it.

```bash
PYTHONPATH=tests .venv/bin/python tests/calibrate.py
```

Re-derives the spread and shrinkage constants from finished seasons. Add
`--sweep` to re-fit the waiver depth and bench factor constants too, which
replays 60 drafts per configuration and is slow.
