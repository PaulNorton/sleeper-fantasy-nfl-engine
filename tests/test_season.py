"""Offline checks on the in-season tools: lineup, waivers, drops, alerts.

Run: PYTHONPATH=tests .venv/bin/python tests/test_season.py
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from fantasy_draft.changes import find_movers, load_snapshot, save_snapshot
from fantasy_draft.lineup import find_swaps
from fantasy_draft.projections import Player, load_week_points
from fantasy_draft.sleeper import SleeperClient
from fantasy_draft.valuation import (
    LineupEvaluator,
    add_to_roster,
    remove_from_roster,
    roster_from_players,
)
from fantasy_draft.waivers import rank_adds, rank_drops
from harness import make_league, load

league = make_league()
players, replacement = load(league)
by_name = {p.name: p for p in players}
evaluator = LineupEvaluator.from_league(league, replacement)


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

# 2 - remove_from_roster undoes add_to_roster exactly.
roster = add_to_roster(add_to_roster({}, "RB", 200.0, bye=5), "WR", 150.0, bye=9)
back = remove_from_roster(roster, "RB", 200.0, bye=5)
assert back == {"RB": [], "WR": [(-150.0, 9)]}, back
print("PASS: remove_from_roster is the exact inverse of add_to_roster")

# 3 - contribution is symmetric with marginal: adding a player and then asking
# his contribution to the resulting roster returns the same number.
base = roster_from_players([mk("a", "RB", 180.0), mk("b", "WR", 140.0)])
gain = evaluator.marginal(base, "RB", 90.0, bye=3)
with_new = add_to_roster(base, "RB", 90.0, bye=3)
back_out = evaluator.contribution(with_new, "RB", 90.0, bye=3)
assert abs(gain - back_out) < 1e-6, (gain, back_out)
print("PASS: contribution() undoes marginal() to the same number")

# 4 - a bench player nobody would start contributes near nothing; the starter
# he sits behind contributes real value.
deep_bench = roster_from_players([
    mk("qb1", "QB", 320.0), mk("qb2", "QB", 200.0), mk("qb3", "QB", 180.0),
])
c1 = evaluator.contribution(deep_bench, "QB", 320.0)
c3 = evaluator.contribution(deep_bench, "QB", 180.0)
assert c1 > c3, (c1, c3)
print(f"PASS: starter contribution ({c1:.1f}) beats deep bench contribution ({c3:.1f})")

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

# 7 - waivers: a clear upgrade over the worst starter ranks near the top; a
# free agent who cannot crack the lineup ranks near the bottom.
roster2 = [mk("myqb", "QB", 300.0), mk("myrb1", "RB", 260.0), mk("myrb2", "RB", 90.0)]
pool = [mk("fa_star_rb", "RB", 240.0), mk("fa_scrub_qb", "QB", 120.0)]
targets = rank_adds(evaluator, roster2, pool)
by_id = {t.player.player_id: t for t in targets}
assert by_id["fa_star_rb"].added_value > by_id["fa_scrub_qb"].added_value
assert by_id["fa_star_rb"].added_value > 0
print("PASS: rank_adds favours the free agent who actually upgrades the lineup")

# 8 - drops: the weakest bench player drops before a starter does.
drop_order = rank_drops(evaluator, roster2)
assert drop_order[0].player.player_id == "myrb2", drop_order
print("PASS: rank_drops lists the weakest bench player first")

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
