"""League settings: roster shape, scoring format, replacement levels."""

from __future__ import annotations

from dataclasses import dataclass, field

BENCH_SLOTS = {"BN", "IR", "TAXI"}

# Which real positions can fill each flex slot, best first.
FLEX_MAP: dict[str, tuple[str, ...]] = {
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "WRRB_WRT": ("RB", "WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
    "IDP_FLEX": ("DL", "LB", "DB"),
}

# How a flex slot's demand splits across the positions that can fill it.
FLEX_SHARE: dict[str, dict[str, float]] = {
    "FLEX": {"RB": 0.45, "WR": 0.45, "TE": 0.10},
    "WRRB_FLEX": {"RB": 0.5, "WR": 0.5},
    "REC_FLEX": {"WR": 0.8, "TE": 0.2},
    "WRRB_WRT": {"RB": 0.45, "WR": 0.45, "TE": 0.10},
    "SUPER_FLEX": {"QB": 0.75, "RB": 0.08, "WR": 0.12, "TE": 0.05},
    "IDP_FLEX": {"DL": 0.34, "LB": 0.33, "DB": 0.33},
}

OFFENSE = ("QB", "RB", "WR", "TE")
KICK_DEF = ("K", "DEF")
IDP = ("DL", "LB", "DB")


@dataclass
class LeagueSettings:
    league_id: str
    name: str
    season: str
    teams: int
    roster_positions: list[str]
    scoring_settings: dict[str, float]
    league_type: int = 0  # 0 redraft, 1 keeper, 2 dynasty

    starter_slots: dict[str, int] = field(default_factory=dict)  # incl. flex slot names
    bench_size: int = 0

    @classmethod
    def from_api(cls, data: dict) -> "LeagueSettings":
        positions = list(data.get("roster_positions") or [])
        settings = data.get("settings") or {}
        obj = cls(
            league_id=str(data.get("league_id", "")),
            name=data.get("name", "league"),
            season=str(data.get("season", "")),
            teams=int(settings.get("num_teams") or data.get("total_rosters") or 12),
            roster_positions=positions,
            scoring_settings={k: float(v) for k, v in (data.get("scoring_settings") or {}).items()},
            league_type=int(settings.get("type", 0) or 0),
        )
        counts: dict[str, int] = {}
        bench = 0
        for slot in positions:
            if slot in BENCH_SLOTS:
                bench += 1
            else:
                counts[slot] = counts.get(slot, 0) + 1
        obj.starter_slots = counts
        obj.bench_size = bench
        return obj

    # ---------- derived properties ----------

    @property
    def rec_points(self) -> float:
        return float(self.scoring_settings.get("rec", 0.0))

    @property
    def scoring_format(self) -> str:
        rec = self.rec_points
        if rec >= 0.75:
            return "ppr"
        if rec >= 0.25:
            return "half_ppr"
        return "std"

    @property
    def superflex(self) -> bool:
        return self.starter_slots.get("SUPER_FLEX", 0) > 0 or self.starter_slots.get("QB", 0) > 1

    @property
    def roster_size(self) -> int:
        return len(self.roster_positions)

    @property
    def adp_key(self) -> str:
        """Which of Sleeper's ADP columns matches this league."""
        dynasty = self.league_type == 2
        if self.superflex:
            return "adp_dynasty_2qb" if dynasty else "adp_2qb"
        fmt = self.scoring_format
        return f"adp_dynasty_{fmt}" if dynasty else f"adp_{fmt}"

    @property
    def needed_positions(self) -> list[str]:
        """Real positions worth pulling projections for."""
        wanted: set[str] = set()
        for slot, n in self.starter_slots.items():
            if slot in FLEX_MAP:
                wanted.update(FLEX_MAP[slot])
            else:
                wanted.add(slot)
        if self.bench_size:
            wanted.update(OFFENSE)
        return [p for p in (*OFFENSE, *KICK_DEF, *IDP) if p in wanted]

    def starters_per_team(self) -> dict[str, float]:
        """Expected starters per team per real position, spreading flex demand."""
        demand: dict[str, float] = {}
        for slot, n in self.starter_slots.items():
            if slot in FLEX_MAP:
                for pos, share in FLEX_SHARE[slot].items():
                    demand[pos] = demand.get(pos, 0.0) + n * share
            else:
                demand[slot] = demand.get(slot, 0.0) + n
        return demand

    def replacement_ranks(self) -> dict[str, int]:
        """Positional rank that counts as replacement level, league-wide."""
        ranks: dict[str, int] = {}
        for pos, per_team in self.starters_per_team().items():
            ranks[pos] = max(1, round(per_team * self.teams))
        return ranks

    def slots_for_position(self, pos: str) -> list[str]:
        """Starter slots this position can legally fill, dedicated first."""
        out = [s for s in self.starter_slots if s == pos]
        out += [s for s in self.starter_slots if s in FLEX_MAP and pos in FLEX_MAP[s]]
        return out

    def describe(self) -> str:
        starters = " ".join(
            f"{n}{slot}" for slot, n in sorted(self.starter_slots.items(), key=lambda kv: -kv[1])
        )
        kind = {0: "redraft", 1: "keeper", 2: "dynasty"}.get(self.league_type, "redraft")
        sf = " superflex" if self.superflex else ""
        return f"{self.teams}-team {kind} {self.scoring_format}{sf} | {starters} | {self.bench_size} bench"


# Sleeper's default offensive scoring, used for mock drafts that have no league.
BASE_SCORING: dict[str, float] = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -2.0,
    "pass_2pt": 2.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rush_2pt": 2.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec_2pt": 2.0,
    "fum_lost": -2.0,
}

_SLOT_KEYS = {
    "slots_qb": "QB",
    "slots_rb": "RB",
    "slots_wr": "WR",
    "slots_te": "TE",
    "slots_flex": "FLEX",
    "slots_super_flex": "SUPER_FLEX",
    "slots_wrrb_flex": "WRRB_FLEX",
    "slots_rec_flex": "REC_FLEX",
    "slots_k": "K",
    "slots_def": "DEF",
    "slots_idp_flex": "IDP_FLEX",
    "slots_dl": "DL",
    "slots_lb": "LB",
    "slots_db": "DB",
    "slots_bn": "BN",
}


def settings_from_draft(draft: dict) -> LeagueSettings:
    """Build league settings from a standalone mock draft that has no league."""
    settings = draft.get("settings") or {}
    metadata = draft.get("metadata") or {}
    positions: list[str] = []
    for key, slot in _SLOT_KEYS.items():
        positions.extend([slot] * int(settings.get(key) or 0))
    if not positions:
        positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6

    scoring = dict(BASE_SCORING)
    fmt = str(metadata.get("scoring_type") or "ppr")
    scoring["rec"] = 1.0 if fmt == "ppr" else 0.5 if fmt == "half_ppr" else 0.0

    league = LeagueSettings(
        league_id="",
        name=metadata.get("name") or "Mock draft",
        season=str(draft.get("season") or ""),
        teams=int(settings.get("teams") or 12),
        roster_positions=positions,
        scoring_settings=scoring,
    )
    counts: dict[str, int] = {}
    bench = 0
    for slot in positions:
        if slot in BENCH_SLOTS:
            bench += 1
        else:
            counts[slot] = counts.get(slot, 0) + 1
    league.starter_slots = counts
    league.bench_size = bench
    return league
