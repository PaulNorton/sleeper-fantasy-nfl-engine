"""Offline checks on the in-season tools: lineup, waivers, alerts.

Run: PYTHONPATH=tests .venv/bin/python tests/test_season.py
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from sleeper_fantasy_nfl_engine.changes import find_movers, load_snapshot, save_snapshot
from sleeper_fantasy_nfl_engine.lineup import find_swaps
from sleeper_fantasy_nfl_engine.projections import Player, load_week_points
from sleeper_fantasy_nfl_engine.sleeper import SleeperClient
from sleeper_fantasy_nfl_engine.valuation import LineupEvaluator
from sleeper_fantasy_nfl_engine.waivers import (
    Horizon,
    best_moves,
    horizon_value,
    open_roster_spots,
    open_slots,
    remaining_weeks,
    shortlist_free_agents,
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

# 8 - the same fill-in is reported as a move that can wait, because he starts
# in one week out of three and that week is not this one. A genuine upgrade is
# not deferred.
moves = best_moves(two_wr, bye_horizon, [wr1, wr2, spare], [plugger], limit=5)
assert moves and moves[0].add.player_id == "plugger", moves
assert moves[0].drop.player_id == "spare_rb", moves[0].drop
assert moves[0].can_wait, "one start in a future week is bye cover, not an upgrade"
assert "wait until week 1" in moves[0].reason(), moves[0].reason()

upgrade_horizon = Horizon(
    weeks=[1, 2, 3],
    points={wk: {"wr1": 18.0, "wr2": 4.0, "plugger": 15.0} for wk in (1, 2, 3)},
)
steady = best_moves(two_wr, upgrade_horizon, [wr1, wr2, spare], [plugger], limit=5)
assert steady and not steady[0].can_wait, "a player who starts every week cannot wait"
assert steady[0].gain > 0, steady[0].gain
print("PASS: a one-week fill-in is deferred; a week-in-week-out upgrade is not")

# 9 - the add and the drop are scored as one decision, so a drop that actually
# costs you is netted off rather than ignored.
costly = best_moves(two_wr, upgrade_horizon, [wr1, wr2], [plugger], limit=5)
assert costly, "with no spare spot a move must still be offered"
assert costly[0].drop.player_id == "wr2", costly[0].drop
assert abs(costly[0].gain - 3 * (15.0 - 4.0)) < 1e-6, costly[0].gain
print("PASS: add and drop are netted as a single decision")

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
