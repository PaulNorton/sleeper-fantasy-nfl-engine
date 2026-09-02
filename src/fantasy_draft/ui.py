"""Plain terminal report. The numbers matter; the layout is deliberately spare."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .draft import DraftState, fill_roster, open_starter_slots
from .league import LeagueSettings
from .projections import Player
from .recommend import Recommendation
from .simulate import availability

POS_STYLE = {
    "QB": "bright_magenta",
    "RB": "bright_green",
    "WR": "bright_cyan",
    "TE": "bright_yellow",
    "K": "grey62",
    "DEF": "grey62",
}


def fmt_score(value: float) -> str:
    """Scores are points under the default objective and probabilities under --objective win."""
    return f"{value:.0f}" if abs(value) >= 5 else f"{value:+.3f}"


def pos_text(pos: str) -> Text:
    return Text(pos, style=POS_STYLE.get(pos.rstrip("0123456789"), "white"))


def header(
    league: LeagueSettings, state: DraftState, my_next: int | None, on_clock: str | None
) -> Text:
    rnd, slot = state.round_and_slot(state.current_pick)
    line = Text()
    line.append(f"{league.name}", style="bold")
    line.append(f"  ({league.describe()})\n", style="dim")
    line.append(f"{state.status} · pick {state.current_pick} (round {rnd}.{slot})")
    if on_clock:
        line.append(f" · on the clock: {on_clock}", style="yellow")
    if my_next:
        away = state.picks_until(my_next)
        r2, s2 = state.round_and_slot(my_next)
        line.append(
            "\nYOU ARE ON THE CLOCK" if away == 0
            else f"\nYour pick {my_next} (round {r2}.{s2}) is {away} picks away",
            style="bold green" if away == 0 else "bold",
        )
    return line


def recommendations_table(recs: list[Recommendation], width: int = 200) -> Table:
    """Columns drop as the terminal narrows, most explanatory first."""
    best = recs[0].score if recs else 0.0
    detail = width >= 96
    why = width >= 118

    table = Table(header_style="bold", title_justify="left", pad_edge=False, padding=(0, 1))
    table.add_column("#", width=2, justify="right")
    table.add_column("Player", min_width=16, no_wrap=True)
    table.add_column("Pos", width=5)
    if detail:
        table.add_column("Tm", width=4)
    table.add_column("Proj", width=4, justify="right")
    table.add_column("VOR", width=4, justify="right")
    if detail:
        table.add_column("Floor", width=5, justify="right")
        table.add_column("Ceil", width=5, justify="right")
        table.add_column("Wait", width=4, justify="right")
    table.add_column("Back", width=4, justify="right")
    if detail:
        table.add_column("Tier", width=5, justify="center")
    table.add_column("Cost", width=6, justify="right")
    if why:
        table.add_column("Why", overflow="fold")

    for i, r in enumerate(recs, start=1):
        p = r.player
        row = [str(i), Text(p.label, style="bold" if i == 1 else ""), pos_text(f"{p.position}{p.pos_rank}")]
        if detail:
            row.append(p.team or "FA")
        row += [f"{p.points:.0f}", f"{r.vor:+.0f}"]
        if detail:
            row += [f"{r.floor:.0f}", f"{r.ceiling:.0f}", f"{r.wait_cost:.0f}"]
        row.append("—" if r.avail_next <= 0 else f"{r.avail_next:.0%}")
        if detail:
            row.append(f"T{r.tier}\u00b7{r.tier_left}")
        row.append(
            Text("best" if i == 1 else fmt_score(r.score - best), style="green" if i == 1 else "dim")
        )
        if why:
            row.append(Text(r.reason, style="dim"))
        table.add_row(*row)
    return table


def verdict(recs: list[Recommendation]) -> Text:
    if not recs:
        return Text("No players left to rate.", style="yellow")
    top = recs[0]
    text = Text()
    text.append("TAKE: ", style="bold")
    text.append(f"{top.player.name} ({top.player.position}, {top.player.team or 'FA'})", style="bold green")
    gap = (
        f"+{top.edge:.1f} expected points"
        if abs(top.score) >= 5
        else f"+{top.edge * 100:.2f}% title odds"
    )
    text.append(f"  {gap} over the next best option", style="dim")
    if top.reason:
        text.append(f"\n      {top.reason}", style="dim")
    return text


def roster_panel(league: LeagueSettings, players: list[Player]) -> Panel:
    seated, bench = fill_roster(league, players)
    pools = {slot: list(v) for slot, v in seated.items()}
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", width=11)
    table.add_column()
    for slot in league.roster_positions:
        if slot in ("BN", "IR", "TAXI"):
            continue
        pool = pools.get(slot) or []
        name = pool.pop(0) if pool else None
        table.add_row(
            slot,
            Text(f"{name.name} ({name.position} {name.points:.0f})") if name else Text("—", style="grey37"),
        )
    if bench:
        table.add_row("BENCH", Text(", ".join(f"{p.name} ({p.position})" for p in bench), style="dim"))
    holes = open_starter_slots(league, players)
    subtitle = "needs " + " ".join(f"{n}×{s}" for s, n in holes.items()) if holes else "starters full"
    return Panel(table, title="Your roster", subtitle=subtitle, border_style="green")


def upcoming_panel(state: DraftState, picks: list[int]) -> Panel:
    text = Text()
    for n in picks:
        rnd, slot = state.round_and_slot(n)
        text.append(f"{rnd}.{slot:02d}  pick {n}  (+{state.picks_until(n)})\n")
    return Panel(text or Text("—"), title="Your next picks", border_style="cyan")


def scarcity_panel(available: list[Player], next_pick: int | None, current_pick: int) -> Panel:
    """Startable supply left per position, and how much of it goes before your turn."""
    counts: dict[str, list[Player]] = {}
    for p in available:
        if p.vorp > 0:
            counts.setdefault(p.position, []).append(p)
    table = Table.grid(padding=(0, 2))
    for col in range(3):
        table.add_column(justify="left" if col == 0 else "right")
    table.add_row(Text("Pos", style="dim"), Text("Left", style="dim"), Text("Gone", style="dim"))
    for pos in ("QB", "RB", "WR", "TE"):
        group = sorted(counts.get(pos, []), key=lambda p: -p.value)
        if not group:
            continue
        gone = ""
        if next_pick:
            gone = f"{sum(1 - availability(p, next_pick, current_pick) for p in group[:30]):.1f}"
        table.add_row(pos_text(pos), str(len(group)), gone)
    return Panel(table, title="Startable supply", border_style="magenta")


def recent_panel(state: DraftState, by_id: dict[str, Player], names: dict[int, str]) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", width=6)
    table.add_column(ratio=1)
    table.add_column(width=14, style="dim")
    for pick in state.recent_picks(10):
        no = int(pick.get("pick_no") or 0)
        rnd, slot = state.round_and_slot(no)
        meta = pick.get("metadata") or {}
        p = by_id.get(str(pick.get("player_id")))
        label = (
            f"{p.name} {p.position}"
            if p
            else f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
        )
        table.add_row(f"{rnd}.{slot:02d}", label, names.get(int(pick.get("roster_id") or 0), ""))
    return Panel(table, title="Recent picks", border_style="grey50")


def build_screen(
    league: LeagueSettings,
    state: DraftState,
    recs: list[Recommendation],
    my_players: list[Player],
    my_picks: list[int],
    available: list[Player],
    by_id: dict[str, Player],
    roster_names: dict[int, str],
    on_clock: str | None,
    width: int = 200,
) -> RenderableType:
    from rich.columns import Columns

    my_next = my_picks[0] if my_picks else None
    following = my_picks[1] if len(my_picks) > 1 else None
    current = my_next or state.current_pick
    return Group(
        header(league, state, my_next, on_clock),
        Text(),
        verdict(recs),
        recommendations_table(recs, width),
        Columns(
            [
                roster_panel(league, my_players),
                upcoming_panel(state, my_picks),
                scarcity_panel(available, following, current),
                recent_panel(state, by_id, roster_names),
            ],
            expand=True,
        ),
        Text(
            "Proj season points · VOR over replacement · Floor/Ceil 10th and 90th percentile "
            "season · Wait points lost if you pass on the position · Back P(available next "
            "turn) · Cost expected points behind the best pick",
            style="dim",
        ),
    )
