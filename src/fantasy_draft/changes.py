"""Notice when a projection moves - the usual sign of an injury, a depth-chart
change, or a role shift nobody announced directly.

Works by snapshotting every rostered-relevant player's season projection after
each run and diffing against the last snapshot on file. There is no news feed
here: a season-ending injury and a beat-writer report of a lost starting job
both show up the same way, as a projection that moved.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .projections import Player

# How far a projection has to move before it is worth surfacing, as a fraction
# of its own prior value. Not fitted to data - a guess at the line between
# normal week-to-week model churn and a change worth a manager's attention. A
# flat points floor rides alongside it so a barely-owned player's projection
# cannot trip the alert on what is really a two-point move.
RELATIVE_THRESHOLD = 0.20
ABSOLUTE_FLOOR = 15.0


@dataclass
class Movement:
    player: Player
    previous: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.previous


def _path(cache_dir: Path, league_id: str, season: str) -> Path:
    # Scoped per league and season: the same player_id carries a different
    # projection under different scoring settings, so a shared snapshot
    # across leagues would read as a false alarm every time you switched.
    return cache_dir / f"projection_snapshot_{league_id}_{season}.json"


def load_snapshot(cache_dir: Path, league_id: str, season: str) -> dict[str, float]:
    path = _path(cache_dir, league_id, season)
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("points", {})


def save_snapshot(cache_dir: Path, league_id: str, season: str, players: list[Player]) -> None:
    data = {"saved_at": time.time(), "points": {p.player_id: p.points for p in players}}
    _path(cache_dir, league_id, season).write_text(json.dumps(data))


def find_movers(
    players: list[Player],
    previous: dict[str, float],
    relative: float = RELATIVE_THRESHOLD,
    floor: float = ABSOLUTE_FLOOR,
) -> list[Movement]:
    """Players whose season projection moved enough since the last snapshot to
    be worth a look. Always empty on the first run - nothing to compare to yet.
    """
    movers = []
    for p in players:
        prior = previous.get(p.player_id)
        if prior is None or prior <= 0:
            continue
        delta = abs(p.points - prior)
        if delta >= floor and delta / prior >= relative:
            movers.append(Movement(p, prior, p.points))
    movers.sort(key=lambda m: -abs(m.delta))
    return movers
