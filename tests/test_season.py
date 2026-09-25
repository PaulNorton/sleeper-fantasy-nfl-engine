"""Offline checks on the in-season tools: lineup, waivers, alerts.

Run: PYTHONPATH=tests .venv/bin/python tests/test_season.py
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from sleeper_fantasy_nfl_engine.changes import find_movers, load_snapshot, save_snapshot
from sleeper_fantasy_nfl_engine.fantasypros import (
    Consensus,
    load_ros_consensus,
    load_weekly_consensus,
)
from sleeper_fantasy_nfl_engine.lineup import Swap, find_swaps
from sleeper_fantasy_nfl_engine.projections import Player, blend_ranks, load_week_points
from sleeper_fantasy_nfl_engine.sleeper import SleeperClient
from sleeper_fantasy_nfl_engine.valuation import LineupEvaluator
from sleeper_fantasy_nfl_engine.waivers import (
    Horizon,
    best_moves,
    droppable_ids,
    horizon_value,
    open_roster_spots,
    open_slots,
    INJURY_HAZARD,
    INJURY_PERSISTENCE,
    WORTH_DOING,
    remaining_weeks,
    shortlist_free_agents,
    simulate,
)
from harness import make_league

league = make_league()


def mk(name, position, points, bye=0):
    return Player(player_id=name, name=name, position=position, team="XX",
                  points=points, adp=1.0, bye=bye)


# 1 - weekly points come back scored and game-status discounted.
with SleeperClient() as client:
    week1 = load_week_points(client, league, "2025", 1)
assert week1, "week 1 projections for 2025 should not be empty"
assert all(p.points >= 0 for p in week1), "weekly points should never go negative"
assert all(p.adp >= 999 for p in week1), "a single week has no ADP"
out_players = [p for p in week1 if p.injury_status == "Out"]
if out_players:
    assert out_players[0].points <= 3.0, "a player ruled Out should score near zero for the week"
print(f"PASS: loaded {len(week1)} week-1 projections, game-status discounted")

# 1b - the weekly and rest-of-season consensus both load, and between them
# cover every position the app values. A missing position here means a
# FantasyPros page changed shape, and the lineup silently falls back to
# Sleeper alone for it.
with SleeperClient() as client:
    weekly = load_weekly_consensus(league.scoring_format, client.cache_dir)
    ros = load_ros_consensus(league.scoring_format, client.cache_dir)
for label, rows in (("weekly", weekly), ("ros", ros)):
    got = {r.position for r in rows}
    assert {"QB", "RB", "WR", "TE", "K", "DEF"} <= got, (label, sorted(got))
    assert any(r.rank_std > 0 for r in rows), f"{label} publishes no expert disagreement"
assert any(r.opponent for r in weekly), "the weekly ranking should carry a fixture"
print(f"PASS: consensus loaded — {len(weekly)} weekly, {len(ros)} rest-of-season")

# 1c - the consensus actually reaches a weekly board, and reaches it before the
# injury discount rather than after. Ordering the other way round would move a
# player's discounted points onto somebody healthy.
with SleeperClient() as client:
    plain = {p.player_id: p for p in load_week_points(client, league, "2025", 1)}
    blended = {
        p.player_id: p
        for p in load_week_points(client, league, "2025", 1, consensus=weekly)
    }
assert set(plain) == set(blended), "blending must not add or drop players"
assert any(r.ecr is not None for r in blended.values()), "no player matched the consensus"
assert any(
    abs(blended[pid].points - plain[pid].points) > 0.05 for pid in plain
), "the consensus changed no ordering at all"
for pid, p in blended.items():
    if p.injury_status in ("Out", "IR"):
        assert p.points <= 3.0, (p.name, p.injury_status, p.points)
print("PASS: weekly consensus is blended in, and the injury discount is applied after it")

# 1d - blend_ranks re-seats a position on its own points curve: same points,
# different owners, and a position the ranking says nothing about is untouched.
board = [mk("a", "WR", 30.0), mk("b", "WR", 20.0), mk("c", "WR", 10.0),
         mk("lone_k", "K", 8.0)]


def rank_row(name, pos, ecr, std=0.0):
    return Consensus(name=name, position=pos, team="XX", ecr=ecr, rank_std=std,
                     rank_min=ecr, rank_max=ecr, tier=1, bye=None)


# The experts invert Sleeper's order, and at weight 1.0 they win outright.
blend_ranks(board, [rank_row("c", "WR", 1), rank_row("b", "WR", 2),
                    rank_row("a", "WR", 3)], weight=1.0)
seated = {p.name: p.points for p in board}
assert seated == {"c": 30.0, "b": 20.0, "a": 10.0, "lone_k": 8.0}, seated
assert next(p for p in board if p.name == "c").pos_rank == 1
assert next(p for p in board if p.name == "lone_k").points == 8.0
print("PASS: blend_ranks re-seats one position's curve and leaves the others alone")

# 1e - a swap the experts do not separate is reported as a coin flip, and one
# they do is not.
close = Swap("WR", replace(mk("x", "WR", 12.0), ecr=10.0, rank_std=4.0),
             replace(mk("y", "WR", 11.5), ecr=12.0, rank_std=4.0))
clear = Swap("WR", replace(mk("x", "WR", 18.0), ecr=4.0, rank_std=1.0),
             replace(mk("y", "WR", 9.0), ecr=40.0, rank_std=2.0))
assert close.close_call and not clear.close_call
assert abs(clear.edge - 9.0) < 1e-6, clear.edge
# An empty slot is a change to make, never a coin flip, however wide the spread.
assert not Swap("WR", replace(mk("x", "WR", 5.0), ecr=90.0, rank_std=30.0), None).close_call
# Nor are two ranks from different rankings comparable just because both are
# small numbers - find_swaps pairs a QB with a defence when nothing else is left.
crossed = Swap("QB", replace(mk("qb", "QB", 18.0), ecr=10.0, rank_std=4.0),
               replace(mk("def", "DEF", 7.0), ecr=11.0, rank_std=4.0))
assert not crossed.close_call
print("PASS: a start/sit call inside the experts' own disagreement is flagged as a coin flip")

# 2 - the horizon only covers the weeks still to be played.
assert remaining_weeks(9)[0] == 9 and remaining_weeks(9)[-1] == 17
assert remaining_weeks(9, 3) == [9, 10, 11], remaining_weeks(9, 3)
print("PASS: the horizon starts at this week and ignores the ones already played")

# 5 - find_swaps: an obviously better bench player should replace a starter.
# A one-WR-slot league, so the two WRs actually compete for the same spot
# rather than both fitting as starters the way the full STANDARD slate would.
one_wr = make_league(positions=["WR", "BN", "BN"])
starter = mk("starter_wr", "WR", 80.0)
bench_star = mk("bench_wr", "WR", 220.0)
roster_players = [starter, bench_star]
swaps = find_swaps(one_wr, roster_players, current_starter_ids={starter.player_id})
assert any(s.start.player_id == bench_star.player_id for s in swaps), swaps
assert any(s.sit and s.sit.player_id == starter.player_id for s in swaps), swaps
print("PASS: find_swaps benches the worse starter for the better bench player")

# 6 - find_swaps agrees when the saved lineup is already optimal.
agree = find_swaps(one_wr, [starter, bench_star], current_starter_ids={bench_star.player_id})
assert agree == [], agree
print("PASS: find_swaps does not touch a slot that is already right")

# 7 - the bye-week case, which is the whole point of scoring week by week.
# A two-WR league, three weeks left, and both receivers are on bye in week 2
# (absent from that week's board). A free agent who plays week 2 should be
# worth real points even though his own numbers are the worst of the three.
two_wr = make_league(positions=["WR", "WR", "BN"])
wr1, wr2 = mk("wr1", "WR", 0.0), mk("wr2", "WR", 0.0)
plugger, spare = mk("plugger", "WR", 0.0), mk("spare_rb", "RB", 0.0)
bye_horizon = Horizon(
    weeks=[1, 2, 3],
    points={
        1: {"wr1": 18.0, "wr2": 16.0, "plugger": 7.0},
        2: {"plugger": 7.0},                              # week 2: both WRs on bye
        3: {"wr1": 18.0, "wr2": 16.0, "plugger": 7.0},
    },
)
zero_floor = LineupEvaluator.from_league(two_wr, {})
holes = open_slots(zero_floor, bye_horizon, [wr1, wr2])
assert holes == {1: 0, 2: 2, 3: 0}, holes
before = horizon_value(zero_floor, bye_horizon, [wr1, wr2])
after = horizon_value(zero_floor, bye_horizon, [wr1, wr2, plugger])
assert after[2] - before[2] == 7.0, (before, after)
assert after[1] == before[1], "he should not displace a better starter in a normal week"
print("PASS: a bye-week hole is scored honestly and a fill-in is credited for plugging it")

# 8 - timing. A fill-in whose only value is a future week is worth the same
# signed now or then, so the date goes to the later week: no roster spot is
# spent early. Whether to wait depends on the wire. With a second fill-in just
# as good, he can wait. Alone, he is signed now, since waiting risks losing
# him. A week-in-week-out upgrade is never deferred.
plugger2 = mk("plugger2", "WR", 0.0)
two_pluggers = Horizon(weeks=[1, 2, 3],
                       points={**bye_horizon.points, 2: {"plugger": 7.0, "plugger2": 7.0}})
moves = best_moves(two_wr, two_pluggers, [wr1, wr2, spare], [plugger, plugger2], limit=5)
assert moves and moves[0].add.player_id in ("plugger", "plugger2"), moves
assert moves[0].drop.player_id == "spare_rb", moves[0].drop
assert moves[0].sign_week == 2 and moves[0].replaceable, moves[0]
assert not moves[0].act_now, "a fill-in the wire can replace should wait for his week"
assert "wait until week 2" in moves[0].reason(), moves[0].reason()
assert len(moves) == 1, "interchangeable fill-ins collapse to one row"

alone = best_moves(two_wr, bye_horizon, [wr1, wr2, spare], [plugger], limit=5)
assert alone and not alone[0].replaceable and alone[0].act_now, alone

upgrade_horizon = Horizon(
    weeks=[1, 2, 3],
    points={wk: {"wr1": 18.0, "wr2": 4.0, "plugger": 15.0} for wk in (1, 2, 3)},
)
steady = best_moves(two_wr, upgrade_horizon, [wr1, wr2, spare], [plugger], limit=5)
assert steady and steady[0].act_now and steady[0].sign_week == 1, steady
assert steady[0].gain > 0, steady[0].gain
print("PASS: a replaceable fill-in waits for his week; a unique one or an upgrade is signed now")

# 8b - a drop that still earns early weeks pushes the best date later. The
# receiver being cut scores 12 in week 1 and 2 after that; the signing scores
# 8 every week. Now is worth +8, week 2 is worth +12. Alone on the wire he is
# still signed now, because +8 already clears the bar; with an equal rival on
# the wire, waiting until week 2 costs nothing.
fading = Horizon(weeks=[1, 2, 3], points={
    1: {"wr1": 18.0, "wr2": 12.0, "plugger": 8.0, "plugger2": 7.5},
    2: {"wr1": 18.0, "wr2": 2.0, "plugger": 8.0, "plugger2": 7.5},
    3: {"wr1": 18.0, "wr2": 2.0, "plugger": 8.0, "plugger2": 7.5},
})
solo_fade = best_moves(two_wr, fading, [wr1, wr2], [plugger], limit=5)[0]
assert solo_fade.drop.player_id == "wr2"
assert abs(solo_fade.gain - 8.0) < 1e-6 and solo_fade.sign_week == 2, solo_fade
assert abs(solo_fade.sign_value - 12.0) < 1e-6, solo_fade.sign_value
assert solo_fade.act_now and "week 2 is worth +4.0 more" in solo_fade.reason(), solo_fade.reason()
rival_fade = best_moves(two_wr, fading, [wr1, wr2], [plugger, plugger2], limit=5)[0]
assert rival_fade.replaceable and not rival_fade.act_now, rival_fade
print("PASS: a drop still earning early weeks moves the signing date later")

# 9 - the add and the drop are scored as one decision, so a drop that actually
# costs you is netted off rather than ignored.
costly = best_moves(two_wr, upgrade_horizon, [wr1, wr2], [plugger], limit=5)
assert costly, "with no spare spot a move must still be offered"
assert costly[0].drop.player_id == "wr2", costly[0].drop
assert abs(costly[0].gain - 3 * (15.0 - 4.0)) < 1e-6, costly[0].gain
print("PASS: add and drop are netted as a single decision")

# 9b - depth. A backup running back who never starts on today's projections
# is free to drop at face value. In the simulated season his starter gets
# hurt, the backup inherits the work, and dropping him has a price.
rb_k = make_league(positions=["RB", "K", "BN"])
rb1, rb2, kicker = mk("rb1", "RB", 0.0), mk("rb2", "RB", 0.0), mk("k1", "K", 0.0)
depth_weeks = list(range(1, 11))
depth = Horizon(
    weeks=depth_weeks,
    points={wk: {"rb1": 15.0, "rb2": 3.0, "k1": 8.0} for wk in depth_weeks},
    info={"rb1": ("RB", "AAA"), "rb2": ("RB", "AAA"), "k1": ("K", "BBB")},
)
face = best_moves(rb_k, depth, [rb1, rb2], [kicker], limit=5)[0]
assert face.drop.player_id == "rb2" and face.drop_cost == 0.0, face
ids = {"rb1", "rb2", "k1"}
runs = simulate(depth, ids, sims=2000)
priced = best_moves(rb_k, depth, [rb1, rb2], [kicker], limit=5, runs=runs)[0]
assert priced.drop.player_id == "rb2" and priced.drop_cost > 3.0, priced
assert priced.gain < priced.add_value, "the drop is netted off the signing"
print(f"PASS: a backup who covers injuries costs {priced.drop_cost:.1f} points to drop, "
      f"not zero")

# 9c - the injury model keeps a running back out about as often as designed,
# and a player meets the same injuries whoever else is being simulated. That
# is what makes two rosters comparable run by run.
out_weeks = sum(1 for run in runs.points for row in run[1:] if row[runs.index["rb1"]] == 0.0)
share = out_weeks / (len(runs.points) * (len(depth_weeks) - 1))
expected = INJURY_HAZARD["RB"] / (INJURY_HAZARD["RB"] + 1 - INJURY_PERSISTENCE)
assert abs(share - expected) < 0.03, (share, expected)
narrow = simulate(depth, {"rb1"}, sims=50)
assert all(a[narrow.index["rb1"]] == b[runs.index["rb1"]]
           for ra, rb in zip(narrow.points, runs.points) for a, b in zip(ra, rb))
print(f"PASS: running backs miss {share:.0%} of weeks, and the draws do not depend on "
      f"who else is simulated")

# 10 - shortlisting keeps the best of every position, not just the best overall.
many = [mk(f"wr{i}", "WR", 0.0) for i in range(30)] + [mk("lone_te", "TE", 0.0)]
big = Horizon(weeks=[1], points={1: {**{f"wr{i}": 20.0 - i * 0.1 for i in range(30)},
                                     "lone_te": 3.0}})
kept = shortlist_free_agents(big, many, per_position=5)
assert any(p.position == "TE" for p in kept), "a thin position must survive the cut"
assert sum(1 for p in kept if p.position == "WR") == 5, kept
print("PASS: the shortlist keeps the best of each position, not the best overall")

# 11 - roster space: players stashed on IR do not occupy an active spot.
ir_league = make_league(positions=["WR", "WR", "BN", "IR"])
assert open_roster_spots(ir_league, {"players": ["a", "b", "c"], "reserve": []}) == 0
assert open_roster_spots(ir_league, {"players": ["a", "b", "c"], "reserve": ["c"]}) == 1
print("PASS: an IR stash frees an active roster spot")

# 11b - and for the same reason he is never the drop that pays for a signing:
# releasing him frees the IR slot, not the bench spot the new player needs.
assert droppable_ids({"players": ["a", "b", "c"], "reserve": ["c"]}) == {"a", "b"}
stashed = best_moves(two_wr, upgrade_horizon, [wr1, wr2, spare], [plugger], limit=5,
                     droppable={"wr1", "wr2"})
assert stashed and all(m.drop.player_id != "spare_rb" for m in stashed), stashed
print("PASS: a player parked on IR is never offered as the drop")

# 9 - alerts: no snapshot yet means no movers; a big projection swing between
# two snapshots is flagged, a small one is not.
with tempfile.TemporaryDirectory() as tmp:
    cache = Path(tmp)
    moved = mk("mover", "WR", 200.0)
    steady = mk("steady", "RB", 150.0)
    assert load_snapshot(cache, "lg", "2026") == {}
    save_snapshot(cache, "lg", "2026", [moved, steady])
    later = [replace(moved, points=120.0), replace(steady, points=153.0)]
    movers = find_movers(later, load_snapshot(cache, "lg", "2026"))
    ids = {m.player.player_id for m in movers}
    assert "mover" in ids and "steady" not in ids, ids
print("PASS: alerts flags a real swing and ignores normal week-to-week noise")

print("\nAll season checks passed.")
