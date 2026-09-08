"""Offline harness: real projections, synthetic league and draft state."""

from __future__ import annotations

import os
import sys

from sleeper_fantasy_nfl_engine.draft import DraftState
from sleeper_fantasy_nfl_engine.league import LeagueSettings
from sleeper_fantasy_nfl_engine.fantasypros import load_consensus
from sleeper_fantasy_nfl_engine.projections import (
    apply_consensus,
    apply_reliability,
    apply_vorp,
    attach_risk,
    load_players,
    replacement_levels,
)
from sleeper_fantasy_nfl_engine.session import load_config
from sleeper_fantasy_nfl_engine.sleeper import SleeperClient


def league_id() -> str:
    """A real league to read scoring settings from. Any public league will do."""
    lid = os.environ.get("SLEEPER_FANTASY_NFL_ENGINE_LEAGUE_ID") or load_config().get("league_id")
    if not lid:
        sys.exit("set SLEEPER_FANTASY_NFL_ENGINE_LEAGUE_ID, or run `sleeper-fantasy-nfl-engine setup` to save a league")
    return str(lid)


STANDARD = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6
SUPERFLEX = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX"] + ["BN"] * 7

PPR = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -2, "pass_2pt": 2,
    "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
    "rec": 1, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2,
}


def make_league(positions=None, teams=12, scoring=None, name="Test league"):
    lg = LeagueSettings(
        league_id="0", name=name, season="2026", teams=teams,
        roster_positions=positions or STANDARD,
        scoring_settings=dict(scoring or PPR),
    )
    counts, bench = {}, 0
    for slot in lg.roster_positions:
        if slot == "BN":
            bench += 1
        else:
            counts[slot] = counts.get(slot, 0) + 1
    lg.starter_slots, lg.bench_size = counts, bench
    return lg


def make_state(teams=12, rounds=15, picks=None):
    draft = {
        "draft_id": "0", "type": "snake", "status": "drafting",
        "settings": {"teams": teams, "rounds": rounds},
        "slot_to_roster_id": {str(i): i for i in range(1, teams + 1)},
        "draft_order": {},
    }
    return DraftState.from_api(draft, picks or [])


def load(league, season="2026", consensus=True):
    """Load projections the way the app does, expert consensus included."""
    with SleeperClient() as client:
        players = load_players(client, league, season)
        if consensus:
            rows = load_consensus(league.scoring_format, league.superflex, client.cache_dir)
            apply_consensus(players, rows)
    replacement = replacement_levels(players, league.replacement_ranks())
    apply_reliability(players, replacement)
    attach_risk(players)
    apply_vorp(players, replacement)
    return players, replacement


def pick_row(pick_no, roster_id, player_id, teams=12):
    return {
        "pick_no": pick_no,
        "round": (pick_no - 1) // teams + 1,
        "roster_id": roster_id,
        "player_id": str(player_id),
        "metadata": {},
    }
