"""Draft state: pick order, who owns which pick, and what each team has taken."""

from __future__ import annotations

from dataclasses import dataclass, field

from .league import LeagueSettings
from .projections import Player


@dataclass
class DraftState:
    draft_id: str
    kind: str              # snake | linear | auction
    status: str            # pre_draft | drafting | paused | complete
    teams: int
    rounds: int
    reversal_round: int
    draft_order: dict[str, int]        # user_id -> slot
    slot_to_roster: dict[int, int]     # slot -> roster_id
    picks: list[dict]
    traded: list[dict] = field(default_factory=list)

    @classmethod
    def from_api(
        cls, draft: dict, picks: list[dict], traded: list[dict] | None = None
    ) -> "DraftState":
        settings = draft.get("settings") or {}
        order = {str(k): int(v) for k, v in (draft.get("draft_order") or {}).items()}
        slot_map = {
            int(k): int(v) for k, v in (draft.get("slot_to_roster_id") or {}).items()
        }
        return cls(
            draft_id=str(draft.get("draft_id", "")),
            kind=str(draft.get("type", "snake")),
            status=str(draft.get("status", "pre_draft")),
            teams=int(settings.get("teams") or len(slot_map) or 12),
            rounds=int(settings.get("rounds") or 15),
            reversal_round=int(settings.get("reversal_round") or 0),
            draft_order=order,
            slot_to_roster=slot_map,
            picks=picks,
            traded=traded or [],
        )

    # ---------- pick order ----------

    @property
    def total_picks(self) -> int:
        return self.teams * self.rounds

    @property
    def current_pick(self) -> int:
        """Overall pick number that is on the clock (1-indexed)."""
        made = max((int(p.get("pick_no") or 0) for p in self.picks), default=0)
        return min(made + 1, self.total_picks)

    def slot_at_pick(self, pick_no: int) -> int:
        """Which draft slot owns overall pick `pick_no` under snake/linear order."""
        rnd = (pick_no - 1) // self.teams + 1
        idx = (pick_no - 1) % self.teams + 1
        if self.kind != "snake":
            return idx
        reverse = rnd % 2 == 0
        if self.reversal_round and rnd >= self.reversal_round:
            reverse = not reverse
        return self.teams - idx + 1 if reverse else idx

    def pick_numbers_for_slot(self, slot: int) -> list[int]:
        return [
            n for n in range(1, self.total_picks + 1) if self.slot_at_pick(n) == slot
        ]

    def _traded_owner(self, rnd: int, roster_id: int) -> int:
        for t in self.traded:
            if int(t.get("round") or 0) == rnd and int(t.get("roster_id") or 0) == roster_id:
                return int(t.get("owner_id") or roster_id)
        return roster_id

    def roster_owning_pick(self, pick_no: int) -> int | None:
        slot = self.slot_at_pick(pick_no)
        roster_id = self.slot_to_roster.get(slot)
        if roster_id is None:
            return None
        rnd = (pick_no - 1) // self.teams + 1
        return self._traded_owner(rnd, roster_id)

    def my_upcoming_picks(self, roster_id: int, limit: int = 6) -> list[int]:
        start = self.current_pick
        out = []
        for n in range(start, self.total_picks + 1):
            if self.roster_owning_pick(n) == roster_id:
                out.append(n)
                if len(out) >= limit:
                    break
        return out

    def picks_until(self, pick_no: int) -> int:
        return max(0, pick_no - self.current_pick)

    # ---------- board state ----------

    def drafted_ids(self) -> set[str]:
        return {str(p.get("player_id")) for p in self.picks if p.get("player_id")}

    def roster_player_ids(self, roster_id: int) -> list[str]:
        return [
            str(p.get("player_id"))
            for p in sorted(self.picks, key=lambda x: int(x.get("pick_no") or 0))
            if int(p.get("roster_id") or 0) == roster_id and p.get("player_id")
        ]

    def recent_picks(self, n: int = 8) -> list[dict]:
        return sorted(self.picks, key=lambda x: -int(x.get("pick_no") or 0))[:n]

    def round_and_slot(self, pick_no: int) -> tuple[int, int]:
        return (pick_no - 1) // self.teams + 1, (pick_no - 1) % self.teams + 1


def fill_roster(
    league: LeagueSettings, players: list[Player]
) -> tuple[dict[str, list[Player]], list[Player]]:
    """Greedily seat players into starter slots; returns (slots, bench)."""
    remaining = {slot: n for slot, n in league.starter_slots.items()}
    seated: dict[str, list[Player]] = {slot: [] for slot in remaining}
    bench: list[Player] = []

    for p in sorted(players, key=lambda x: -x.points):
        placed = False
        for slot in league.slots_for_position(p.position):
            if remaining.get(slot, 0) > 0:
                seated[slot].append(p)
                remaining[slot] -= 1
                placed = True
                break
        if not placed:
            bench.append(p)
    return seated, bench


def open_starter_slots(league: LeagueSettings, players: list[Player]) -> dict[str, int]:
    """Starter slots still unfilled after seating the current roster."""
    seated, _ = fill_roster(league, players)
    return {
        slot: league.starter_slots[slot] - len(seated.get(slot, []))
        for slot in league.starter_slots
        if league.starter_slots[slot] - len(seated.get(slot, [])) > 0
    }


def position_counts(players: list[Player]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in players:
        counts[p.position] = counts.get(p.position, 0) + 1
    return counts
