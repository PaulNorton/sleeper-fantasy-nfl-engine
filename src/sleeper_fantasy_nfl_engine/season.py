"""Wire league settings, live rosters and season projections for in-season use.

Distinct from `session.Session`, which is built around a draft in progress.
Once the draft is over there is no pick order left to track; a team's roster
comes from `/league/{id}/rosters` instead of draft picks, and it moves week to
week through waivers, drops and trades that a draft session never sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .fantasypros import load_consensus
from .league import LeagueSettings
from .projections import (
    Player,
    apply_consensus,
    apply_overrides,
    apply_reliability,
    apply_vorp,
    attach_risk,
    load_players,
    replacement_levels,
)
from .session import resolve_user_id
from .sleeper import SleeperClient, SleeperError


@dataclass
class SeasonSession:
    client: SleeperClient
    league: LeagueSettings
    season: str
    players: dict[str, Player]          # by player_id, season-long value
    replacement: dict[str, float]
    rosters: list[dict] = field(default_factory=list)   # raw /league/{id}/rosters rows
    roster_names: dict[int, str] = field(default_factory=dict)
    my_roster_id: int | None = None

    def roster_row(self, roster_id: int) -> dict | None:
        return next((r for r in self.rosters if int(r["roster_id"]) == roster_id), None)

    def my_roster(self) -> dict | None:
        return self.roster_row(self.my_roster_id) if self.my_roster_id is not None else None

    def rostered_ids(self) -> set[str]:
        """Everyone on any team in the league - what a free-agent search excludes."""
        ids: set[str] = set()
        for r in self.rosters:
            ids.update(str(pid) for pid in (r.get("players") or []))
        return ids

    def my_players(self) -> list[Player]:
        row = self.my_roster()
        if not row:
            return []
        return [self.players[pid] for pid in (row.get("players") or []) if pid in self.players]

    def my_starter_ids(self) -> set[str]:
        row = self.my_roster()
        if not row:
            return set()
        return {str(pid) for pid in (row.get("starters") or []) if pid and pid != "0"}

    def free_agents(self) -> list[Player]:
        taken = self.rostered_ids()
        return [p for p in self.players.values() if p.player_id not in taken]


def build_season_session(
    client: SleeperClient,
    *,
    username: str | None = None,
    user_id: str | None = None,
    league_id: str | None = None,
    season: str | None = None,
    roster_id: int | None = None,
    overrides: str | None = None,
    consensus: bool = True,
) -> SeasonSession:
    if not league_id:
        raise SleeperError("need a league id (run `sleeper-fantasy-nfl-engine setup`)")
    uid = resolve_user_id(client, username, user_id)
    league_data = client.league(league_id)
    if not league_data:
        raise SleeperError(f"no Sleeper league with id {league_id!r}")
    league = LeagueSettings.from_api(league_data)
    season = season or league.season or str(client.state().get("season") or "")

    players = load_players(client, league, season)
    if consensus:
        try:
            rows = load_consensus(league.scoring_format, league.superflex, client.cache_dir)
            apply_consensus(players, rows)
        except Exception:
            pass
    if overrides:
        apply_overrides(players, Path(overrides))
    replacement = replacement_levels(players, league.replacement_ranks())
    apply_reliability(players, replacement)
    attach_risk(players)
    apply_vorp(players, replacement)

    users = {str(u["user_id"]): u.get("display_name", "?") for u in client.league_users(league_id)}
    rosters = client.league_rosters(league_id)
    roster_names: dict[int, str] = {}
    my_id = roster_id
    for r in rosters:
        rid = int(r["roster_id"])
        roster_names[rid] = users.get(str(r.get("owner_id")), f"roster {rid}")
        if my_id is None and uid and str(r.get("owner_id")) == uid:
            my_id = rid

    return SeasonSession(
        client=client,
        league=league,
        season=season,
        players={p.player_id: p for p in players},
        replacement=replacement,
        rosters=rosters,
        roster_names=roster_names,
        my_roster_id=my_id,
    )
