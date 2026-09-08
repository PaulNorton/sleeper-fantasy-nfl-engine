"""Weekly start/sit: where the optimal lineup disagrees with the one you set.

Reuses the same greedy slot-filler `draft.fill_roster` uses for the draft
board, pointed at this week's projections (`projections.load_week_points`)
instead of season totals.
"""

from __future__ import annotations

from dataclasses import dataclass

from .draft import fill_roster
from .league import LeagueSettings
from .projections import Player


@dataclass
class Swap:
    position: str
    start: Player            # who the optimal lineup starts
    sit: Player | None       # who your saved lineup has instead, if anyone


def optimal_lineup(
    league: LeagueSettings, roster: list[Player]
) -> tuple[dict[str, list[Player]], list[Player]]:
    """This week's best lineup from this week's projections: (starters by slot, bench)."""
    return fill_roster(league, roster)


def find_swaps(
    league: LeagueSettings, roster: list[Player], current_starter_ids: set[str]
) -> list[Swap]:
    """Where the optimal lineup differs from the one Sleeper has saved.

    Matched one for one by position where possible, so a swap reads as a
    straight substitution ("start X over Y") rather than a shuffle. Slot
    identity (which literal FLEX) is not tracked - two lineups can agree on
    every player and still disagree on which numbered slot he sits in, which
    is not a decision anyone needs to see.
    """
    seated, _bench = fill_roster(league, roster)
    optimal_ids = {p.player_id for lst in seated.values() for p in lst}
    by_id = {p.player_id: p for p in roster}

    should_start = sorted(
        (by_id[pid] for pid in optimal_ids - current_starter_ids if pid in by_id),
        key=lambda p: -p.points,
    )
    should_sit = [by_id[pid] for pid in current_starter_ids - optimal_ids if pid in by_id]

    swaps: list[Swap] = []
    remaining = list(should_sit)
    for incoming in should_start:
        match = next((p for p in remaining if p.position == incoming.position), None)
        if match is None and remaining:
            match = remaining[0]
        if match is not None:
            remaining.remove(match)
        swaps.append(Swap(position=incoming.position, start=incoming, sit=match))
    return swaps
