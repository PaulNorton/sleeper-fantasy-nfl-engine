"""Wire config, league, draft and projections into one object the commands use."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .draft import DraftState
from .fantasypros import load_consensus
from .league import LeagueSettings, settings_from_draft
from .projections import (
    Player,
    apply_consensus,
    apply_overrides,
    apply_reliability,
    attach_risk,
    apply_vorp,
    load_players,
    replacement_levels,
)
from .sleeper import SleeperClient, SleeperError

CONFIG_PATH = Path("fdraft.json")


def load_config(path: Path = CONFIG_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_config(data: dict, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


@dataclass
class Session:
    client: SleeperClient
    league: LeagueSettings
    state: DraftState
    players: list[Player]
    replacement: dict[str, float] = field(default_factory=dict)
    consensus_matched: int = 0
    user_id: str | None = None
    my_roster_id: int | None = None
    roster_names: dict[int, str] = field(default_factory=dict)

    @property
    def by_id(self) -> dict[str, Player]:
        return {p.player_id: p for p in self.players}

    def refresh_picks(self) -> None:
        """Re-read the draft. The pick order can still change until it starts."""
        self.state.picks = self.client.draft_picks(self.state.draft_id)
        draft = self.client.draft(self.state.draft_id) or {}
        if draft.get("status"):
            self.state.status = str(draft["status"])
        slots = draft.get("slot_to_roster_id") or {}
        if slots:
            self.state.slot_to_roster = {int(k): int(v) for k, v in slots.items()}
        order = draft.get("draft_order") or {}
        if order:
            self.state.draft_order = {str(k): int(v) for k, v in order.items()}
        if self.user_id and self.user_id in self.state.draft_order:
            slot = self.state.draft_order[self.user_id]
            self.my_roster_id = self.state.slot_to_roster.get(slot, self.my_roster_id)

    def available(self) -> list[Player]:
        drafted = self.state.drafted_ids()
        return [p for p in self.players if p.player_id not in drafted]

    def my_players(self) -> list[Player]:
        if self.my_roster_id is None:
            return []
        by_id = self.by_id
        return [by_id[pid] for pid in self.state.roster_player_ids(self.my_roster_id) if pid in by_id]

    def my_picks(self, limit: int = 5) -> list[int]:
        if self.my_roster_id is None:
            return []
        return self.state.my_upcoming_picks(self.my_roster_id, limit=limit)

    def on_clock(self) -> str | None:
        roster_id = self.state.roster_owning_pick(self.state.current_pick)
        if roster_id is None:
            return None
        return self.roster_names.get(roster_id, f"roster {roster_id}")


def resolve_user_id(client: SleeperClient, username: str | None, user_id: str | None) -> str | None:
    if user_id:
        return str(user_id)
    if not username:
        return None
    user = client.user(username)
    if not user:
        hint = " (that looks like an email; Sleeper needs your username handle)" if "@" in username else ""
        raise SleeperError(f"no Sleeper user named {username!r}{hint}")
    return str(user["user_id"])


def pick_draft(client: SleeperClient, league_id: str) -> dict:
    """The league's live draft, or its most recent one.

    The league drafts list returns abbreviated objects with no slot_to_roster_id,
    so the chosen draft is always re-fetched in full.
    """
    drafts = client.league_drafts(league_id)
    if not drafts:
        raise SleeperError("this league has no draft yet")
    live = [d for d in drafts if d.get("status") in ("drafting", "paused")]
    pool = live or drafts
    chosen = max(pool, key=lambda d: int(d.get("start_time") or d.get("created") or 0))
    return client.draft(str(chosen["draft_id"])) or chosen


def build_session(
    client: SleeperClient,
    *,
    username: str | None = None,
    user_id: str | None = None,
    league_id: str | None = None,
    draft_id: str | None = None,
    season: str | None = None,
    slot: int | None = None,
    overrides: str | None = None,
    consensus: bool = True,
) -> Session:
    uid = resolve_user_id(client, username, user_id)
    season = season or str(client.state().get("season") or "")

    if draft_id:
        draft = client.draft(draft_id)
        if not draft:
            raise SleeperError(f"no draft with id {draft_id}")
        league_id = league_id or draft.get("league_id")
    else:
        if not league_id:
            raise SleeperError("need a league id or a draft id (run `fdraft setup`)")
        draft = pick_draft(client, league_id)

    league_data = client.league(league_id) if league_id else None
    league = LeagueSettings.from_api(league_data) if league_data else settings_from_draft(draft)
    season = season or league.season or str(draft.get("season") or "")

    picks = client.draft_picks(str(draft["draft_id"]))
    traded = client.traded_picks(str(draft["draft_id"]))
    state = DraftState.from_api(draft, picks, traded)

    players = load_players(client, league, season)
    consensus_matched = 0
    if consensus:
        try:
            rows = load_consensus(
                league.scoring_format, league.superflex, client.cache_dir
            )
            consensus_matched = apply_consensus(players, rows)
        except Exception:
            consensus_matched = 0
    if overrides:
        apply_overrides(players, Path(overrides))
    replacement = replacement_levels(players, league.replacement_ranks())
    apply_reliability(players, replacement)
    attach_risk(players)
    apply_vorp(players, replacement)

    roster_names: dict[int, str] = {}
    my_roster_id: int | None = None
    if league_id:
        users = {str(u["user_id"]): u.get("display_name", "?") for u in client.league_users(league_id)}
        for roster in client.league_rosters(league_id):
            rid = int(roster["roster_id"])
            roster_names[rid] = users.get(str(roster.get("owner_id")), f"roster {rid}")
            if uid and str(roster.get("owner_id")) == uid:
                my_roster_id = rid

    if slot:
        my_roster_id = state.slot_to_roster.get(int(slot), my_roster_id)
    elif my_roster_id is None and uid and uid in state.draft_order:
        my_roster_id = state.slot_to_roster.get(state.draft_order[uid], my_roster_id)

    for rid in state.slot_to_roster.values():
        roster_names.setdefault(rid, f"roster {rid}")

    return Session(
        client=client,
        league=league,
        state=state,
        players=players,
        replacement=replacement,
        consensus_matched=consensus_matched,
        user_id=uid,
        my_roster_id=my_roster_id,
        roster_names=roster_names,
    )
