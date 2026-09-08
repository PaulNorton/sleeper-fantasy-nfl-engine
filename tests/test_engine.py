"""Offline checks on the pick engine. Run: PYTHONPATH=tests python tests/test_engine.py"""

import time

from sleeper_fantasy_nfl_engine.projections import attach_risk
from sleeper_fantasy_nfl_engine.recommend import recommend
from sleeper_fantasy_nfl_engine.simulate import RolloutConfig, availability
from harness import SUPERFLEX, load, make_league, make_state, pick_row

league = make_league()
players, replacement = load(league)
by_name = {p.name: p for p in players}
CFG = RolloutConfig(sims=60)


def take(*names):
    return [by_name[n] for n in names]


def board(roster_players, opponent_count, my_picks, lg=league, pool=players, rep=replacement,
          cfg=None):
    picks, n = [], 0
    for p in roster_players:
        n += 1
        picks.append(pick_row(n, 1, p.player_id))
    for p in sorted(pool, key=lambda x: x.adp)[:opponent_count]:
        if p in roster_players:
            continue
        n += 1
        picks.append(pick_row(n, 5, p.player_id))
    state = make_state(picks=picks)
    return recommend(lg, state, pool, rep, 1, my_picks, cfg=cfg or CFG)


def show(title, recs, elapsed=None):
    print(f"\n== {title} ==" + (f"  ({elapsed:.2f}s)" if elapsed else ""))
    best = recs[0].score
    for i, r in enumerate(recs[:8], 1):
        p = r.player
        print(
            f"{i:>2}. {p.name:<22} {p.position}{p.pos_rank:<3} proj {p.points:6.1f} "
            f"vor {r.vor:+6.1f} now {r.lineup_gain:6.1f} wait {r.wait_cost:5.1f} "
            f"back {r.avail_next:4.0%} cost {r.score - best:6.1f} | {r.reason}"
        )


print(f"scored {len(players)} players")
print("replacement:", {k: round(v) for k, v in sorted(replacement.items())})

# 1 - opening pick from an empty roster
t = time.time()
r1 = board([], 0, [1, 24, 25, 48, 49, 72, 73, 96])
show("12-team PPR, pick 1.01", r1, time.time() - t)

# 2 - three QBs already rostered in a one-QB league
r2 = board(take("Josh Allen", "Lamar Jackson", "Jalen Hurts"), 25, [40, 57, 64, 81, 88, 105])
show("Holding 3 QBs in a 1QB league", r2)
assert r2[0].player.position != "QB", "recommended a fourth quarterback"
assert all(r.player.position != "QB" for r in r2[:4]), "QBs still crowding the top"
print("PASS: stops drafting quarterbacks")

# 3 - superflex should pull quarterbacks up the board
sf = make_league(SUPERFLEX, name="Superflex")
sf_players, sf_rep = load(sf)
r3 = board([], 0, [1, 24, 25, 48, 49, 72], lg=sf, pool=sf_players, rep=sf_rep)
show("Superflex, pick 1.01", r3)
def first_qb(recs):
    """Rank of the first QB, or one past the end when none is worth listing."""
    return next((i for i, r in enumerate(recs) if r.player.position == "QB"), len(recs))

qb_sf, qb_std = first_qb(r3), first_qb(r1)
assert qb_sf < qb_std, "superflex did not raise QB value"
# Only the comparative claim is asserted. Where a superflex QB1 belongs in
# absolute terms is conventional wisdom, not something the backtest covers -
# it replays 1QB leagues only.
assert qb_sf < 10, "a superflex QB1 should still be an early pick"
where = f"#{qb_std + 1}" if qb_std < len(r1) else f"not in the top {len(r1)}"
print(f"PASS: first QB at #{qb_sf + 1} superflex vs {where} in 1QB")

# 3b - the risk model must separate a rookie from a veteran of equal projection
pool3b = [p for p in players if p.position in ("RB", "WR") and 120 < p.value < 260]
rookies = [p for p in pool3b if p.years_exp == 0]
vets = [p for p in pool3b if (p.years_exp or 0) >= 4]
def relative_span(p):
    return (p.ceiling - p.floor) / max(p.median, 1.0)
rk = sum(relative_span(p) for p in rookies) / max(1, len(rookies))
vt = sum(relative_span(p) for p in vets) / max(1, len(vets))
print(f"\nrelative range: {len(rookies)} rookies {rk:.2f} vs {len(vets)} veterans {vt:.2f}")
assert rk > vt, "rookies should carry a wider range of outcomes than veterans"
assert all(p.floor < p.median < p.ceiling for p in pool3b[:40]), "percentiles out of order"
print("PASS: rookies price wider than veterans of the same projection")

# 3c - expert disagreement must widen the range on its own
split = max(pool3b, key=lambda p: p.rank_std)
agreed = min((p for p in pool3b if p.value > 150), key=lambda p: p.rank_std)
print(f"experts split on {split.name} (std {split.rank_std:.0f}): range {relative_span(split):.2f}; "
      f"agreed on {agreed.name} (std {agreed.rank_std:.0f}): range {relative_span(agreed):.2f}")
assert relative_span(split) > relative_span(agreed), "expert disagreement did not widen the range"
print("PASS: expert disagreement widens the range")

# 3d - the win objective returns real probabilities and separates candidates
r3d = board(take("Jahmyr Gibbs", "Puka Nacua", "Ja'Marr Chase"), 40, [45, 56, 65, 76],
            cfg=RolloutConfig(sims=300, objective="win"))
shares = [x.score for x in r3d]
print(f"title share spread: {min(shares):.4f}-{max(shares):.4f}, "
      f"{len({round(x, 6) for x in shares})} distinct")
assert all(0.0 < x < 1.0 for x in shares), "title shares must be probabilities"
assert len({round(x, 6) for x in shares}) > 3, "win objective does not separate candidates"
print("PASS: --objective win produces usable title odds")

# 4 - two picks left, kicker and defence still empty
roster4 = take(
    "Josh Allen", "Jahmyr Gibbs", "Bijan Robinson", "Puka Nacua", "Ja'Marr Chase",
    "Brock Bowers", "James Cook", "Jaxon Smith-Njigba", "Trey McBride",
    "De'Von Achane", "Jonathan Taylor", "Christian McCaffrey", "Lamar Jackson",
)
r4 = board(roster4, 140, [170, 179])
show("Two picks left, no K and no DEF", r4)
assert r4[0].player.position in ("K", "DEF"), "ignored a mandatory empty slot"
print("PASS: fills mandatory slots before the draft ends")

# 5 - a player who fell far past his ADP must not look permanently available
gibbs = by_name["Jahmyr Gibbs"]
early = availability(gibbs, 20, 1)
fallen = availability(gibbs, 60, 40)
print(f"\nGibbs available: pick 1->20 {early:.0%}, pick 40->60 {fallen:.0%}")
assert fallen < 0.35, "a faller is still modelled as lasting forever"
print("PASS: fallers get taken quickly")

# 6 - availability must respond to how far away your pick is
state6 = make_state(picks=[pick_row(i + 1, 5, p.player_id)
                           for i, p in enumerate(sorted(players, key=lambda x: x.adp)[:24])])
soon = recommend(league, state6, players, replacement, 1, [25, 45, 52], cfg=CFG)
later = recommend(league, state6, players, replacement, 1, [39, 45, 52], cfg=CFG)
print(f"\nTop pick {soon[0].player.name}: {soon[0].avail_next:.0%} to last from pick 25, "
      f"{later[0].avail_next:.0%} from pick 39")
assert later[0].avail_next > soon[0].avail_next
print("PASS: availability tracks the distance to your pick")

# 7 - the report renders without a live draft
import io

from rich.console import Console

from sleeper_fantasy_nfl_engine.ui import build_screen

buf = Console(file=io.StringIO(), width=200)
buf.print(build_screen(league, state6, soon, [], [25, 45, 52], players,
                       {p.player_id: p for p in players}, {5: "rival"}, "rival"))
assert "TAKE:" in buf.file.getvalue()
print("PASS: report renders")

# 8 - the recommendation must beat a plain best-available heuristic on lineup value
mid = board(take("Jahmyr Gibbs", "Bijan Robinson", "Christian McCaffrey"), 30, [45, 60, 69, 84, 93])
show("Three RBs rostered, pick 45", mid)
assert mid[0].player.position != "RB", "kept stacking running backs with a full backfield"
print("PASS: rotates off a saturated position")

# 9 - identical players, different ADP: who does each strategy take?
# Runs last because it edits projections in place.
wrs = sorted([p for p in players if p.position == "WR"], key=lambda p: -p.points)
scarce, lasts = wrs[12], wrs[13]
scarce.points, scarce.adp, scarce.shrunk = 300.0, 20.0, None
lasts.points, lasts.adp, lasts.shrunk = 300.0, 55.0, None
attach_risk(players)  # projections changed, so ranks and spreads must be rebuilt
state9 = make_state(
    picks=[
        pick_row(i + 1, 5, p.player_id)
        for i, p in enumerate(sorted(players, key=lambda x: x.adp)[:19])
        if p not in (scarce, lasts)
    ]
)
print(f"\n== Equal projections, ADP 20 vs ADP 55 ==")
gaps = {}
for strategy in ("vona", "lineup", "simulate"):
    r9 = recommend(league, state9, players, replacement, 1, [20, 45, 52, 69, 76],
                   cfg=RolloutConfig(sims=300, strategy=strategy))
    i_scarce = next(i for i, r in enumerate(r9) if r.player is scarce)
    i_lasts = next(i for i, r in enumerate(r9) if r.player is lasts)
    gap = r9[i_scarce].score - r9[i_lasts].score
    gaps[strategy] = gap
    print(f"  {strategy:<9} scarce #{i_scarce + 1}, lasts #{i_lasts + 1}, "
          f"gap {gap:+.1f}")

# Only the rollout sees that taking the scarce one lets you have both. The
# closed-form strategies compare against the position's best next turn, and with
# two identical players that is the same number either way. Known gap, and the
# reason --strategy simulate is kept even though it backtests worse.
assert gaps["simulate"] > 5.0, "the rollout should prefer the player who will not last"
assert abs(gaps["vona"]) < 5.0, (
    "vona now separates these; the documented limitation is stale and the README "
    "and _direct_scores comments need updating"
)
print("PASS: the rollout separates them; the closed-form strategies do not, as documented")

# 10 - the compare view: builds cleanly on agreement, and reports a real split
# rather than papering over one. Reuses state9's forced ADP-20-vs-55 pair,
# where strategies are known to disagree (check 9, above).
from sleeper_fantasy_nfl_engine import ui as _ui

state10 = make_state()
agree = {
    strat: recommend(league, state10, players, replacement, 1, [1, 24, 25, 48, 49, 72],
                     cfg=RolloutConfig(sims=200, strategy=strat))
    for strat in ("lineup", "vona", "simulate")
}
table, strategies = _ui.compare_table(agree, limit=8)
verdict = _ui.compare_verdict(agree)
assert strategies == ["lineup", "vona", "simulate"]
assert "AGREE" in verdict.plain, "all three should open on the same player at pick 1.01"
print(f"\ncompare at pick 1.01: {verdict.plain}")
print("PASS: compare view renders and reports agreement")

split = {
    strat: recommend(league, state9, players, replacement, 1, [20, 45, 52, 69, 76],
                     cfg=RolloutConfig(sims=200, strategy=strat))
    for strat in ("lineup", "vona", "simulate")
}
verdict2 = _ui.compare_verdict(split)
assert "SPLIT" in verdict2.plain, "simulate ranks the scarce player above lineup/vona here"
print(f"compare on a known split: {verdict2.plain.splitlines()[0]}")
print("PASS: compare view reports a real disagreement rather than hiding it")

# watch draws the board from results[primary], so every flag combination must
# leave the primary strategy actually computed. Filtering the primary out of the
# fast list shipped a KeyError on the default `sleeper-fantasy-nfl-engine watch`.
from sleeper_fantasy_nfl_engine.cli import _watch_strategies

for _strategy in ("lineup", "vona", "simulate"):
    for _objective in ("points", "win"):
        _primary, _fast = _watch_strategies(_strategy, _objective)
        assert _primary in _fast or _primary == "simulate", (
            f"--strategy {_strategy} --objective {_objective} ranks by {_primary}, "
            f"which nothing computes: fast={_fast}"
        )
    assert _watch_strategies(_strategy, "win") == ("simulate", [])
print("PASS: watch ranks whichever strategy it draws the board from")

# The simulate column must never pass an earlier pick's ranking off as current.
_stale = _ui.simulate_note(picks_made=62, ranked_at=60, running_since=None)
_running = _ui.simulate_note(picks_made=62, ranked_at=60, running_since=time.time() - 5)
_current = _ui.simulate_note(picks_made=60, ranked_at=60, running_since=None)
assert "2 picks behind" in _stale.plain, _stale.plain
assert "ranking" in _running.plain and "behind" in _running.plain, _running.plain
assert _current.plain.strip() == "· simulate current", _current.plain
print("PASS: the simulate column reports how far behind the board it is")
