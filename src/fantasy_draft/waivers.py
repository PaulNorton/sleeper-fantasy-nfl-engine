"""Waiver adds and drops: what a free agent is worth to your roster, and what
you would give up to make room for him.

Both questions use the same yardstick - the change in
`valuation.LineupEvaluator.utility` for your actual roster - rather than a
free agent's raw projection or a bench player's. A free agent who only ever
fills your worst bench slot is worth little even if his own number is fine;
a bench player who is your only cover at a shallow position is worth more
than his own number too.
"""

from __future__ import annotations

from dataclasses import dataclass

from .projections import Player
from .valuation import LineupEvaluator, roster_from_players


@dataclass
class WaiverTarget:
    player: Player
    added_value: float     # utility gained by adding him to your roster as-is


@dataclass
class DropCandidate:
    player: Player
    contribution: float    # utility you would lose by dropping him


def rank_adds(
    evaluator: LineupEvaluator,
    roster: list[Player],
    free_agents: list[Player],
    limit: int = 25,
) -> list[WaiverTarget]:
    """Free agents ranked by what they would add to your roster today."""
    base_roster = roster_from_players(roster)
    base = evaluator.utility(base_roster)
    targets = [
        WaiverTarget(
            p,
            evaluator.marginal(base_roster, p.position, p.value, base=base, bye=p.bye or 0),
        )
        for p in free_agents
    ]
    targets.sort(key=lambda t: -t.added_value)
    return targets[:limit]


def rank_drops(
    evaluator: LineupEvaluator,
    roster: list[Player],
    limit: int = 25,
) -> list[DropCandidate]:
    """Your own roster ranked by how little each player is contributing -
    the safest players to drop first."""
    base_roster = roster_from_players(roster)
    candidates = [
        DropCandidate(
            p, evaluator.contribution(base_roster, p.position, p.value, p.bye or 0)
        )
        for p in roster
    ]
    candidates.sort(key=lambda c: c.contribution)
    return candidates[:limit]
