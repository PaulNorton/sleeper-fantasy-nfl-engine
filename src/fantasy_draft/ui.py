"""Plain terminal report. The numbers matter; the layout is deliberately spare."""

from __future__ import annotations

import time

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


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m {seconds % 60:.0f}s"
    return f"{seconds // 3600:.0f}h {(seconds % 3600) // 60:.0f}m"


def freshness(
    picks_made: int,
    fetched_at: float,
    changed_at: float,
    *,
    edge_age: int = 0,
    interval: float = 0.0,
    error: str | None = None,
) -> Text:
    """The line that answers 'is what I am looking at current?'.

    `edge_age` is the age Sleeper's CDN reported for the copy it served. It
    should be zero because the live endpoints are fetched past the edge cache;
    anything else means the board may be behind the draft, so say so.
    """
    now = time.time()
    line = Text()
    line.append(f"{picks_made} picks made", style="bold")
    line.append(f" · read {time.strftime('%H:%M:%S', time.localtime(fetched_at))}")
    line.append(f" ({ago(now - fetched_at)} ago)")
    if edge_age > 0:
        line.append(f" · CDN copy, {edge_age}s stale", style="yellow")
    else:
        line.append(" · live from Sleeper", style="green")
    if picks_made:
        line.append(f" · last new pick {ago(now - changed_at)} ago", style="dim")
    if interval:
        line.append(f" · polling every {interval:g}s", style="dim")
    if error:
        line.append(f"\nlast poll failed: {error}", style="red")
    return line


def simulate_note(
    picks_made: int,
    ranked_at: int | None,
    running_since: float | None = None,
    error: str | None = None,
) -> Text:
    """How current the simulate column is.

    It ranks in seconds, not milliseconds, so it lands a beat after the rest of
    the board. `ranked_at` is the pick count it last finished on; comparing that
    with `picks_made` says whether the column describes the board on screen.
    """
    text = Text()
    if error:
        text.append(" · simulate failed: ", style="red")
        text.append(error, style="red")
        return text
    if running_since is not None:
        behind = picks_made - ranked_at if ranked_at is not None else None
        text.append(f" · simulate ranking {ago(time.time() - running_since)}", style="yellow")
        if behind:
            text.append(f", column {behind} pick{'s' if behind > 1 else ''} behind", style="yellow")
        elif ranked_at is None:
            text.append(", no column yet", style="yellow")
        return text
    if ranked_at is None:
        text.append(" · simulate not run", style="dim")
    elif ranked_at != picks_made:
        behind = picks_made - ranked_at
        text.append(f" · simulate {behind} pick{'s' if behind > 1 else ''} behind", style="yellow")
    else:
        text.append(" · simulate current", style="green")
    return text


def recommendations_table(
    recs: list[Recommendation],
    width: int = 200,
    alt: dict[str, list[Recommendation]] | None = None,
    columns: tuple[str, ...] = (),
    stale: tuple[str, ...] = (),
) -> Table:
    """Columns drop as the terminal narrows, most explanatory first.

    `columns` names the other strategies to show a rank column for, giving that
    strategy's own rank for the same player. A column listed there but missing
    from `alt` is still being computed and shows as an ellipsis; one named in
    `stale` describes an earlier pick and is marked and dimmed rather than
    quietly passed off as current.
    """
    best = recs[0].score if recs else 0.0
    detail = width >= 96
    why = width >= 118
    alt = alt or {}
    show = list(columns) if width >= 110 else []
    alt_rank = {
        strat: {r.player.player_id: i + 1 for i, r in enumerate(alt.get(strat) or [])}
        for strat in show
    }

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
    for strat in show:
        label = STRATEGY_LABEL.get(strat, strat)[:4]
        table.add_column(f"{label}*" if strat in stale else label, width=5, justify="right")
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
        for strat in show:
            if strat not in alt:
                row.append(Text("…", style="grey37"))
                continue
            n = alt_rank[strat].get(p.player_id)
            style = "yellow" if strat in stale else ""
            row.append(Text("—", style="grey37") if n is None else Text(f"#{n}", style=style))
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


STRATEGY_ORDER = ("lineup", "vona", "simulate")
STRATEGY_LABEL = {"lineup": "Lineup", "vona": "Vona", "simulate": "Simulate"}


def compare_table(
    results: dict[str, list[Recommendation]], limit: int
) -> tuple[Table, list[str]]:
    """One row per candidate, one column per strategy showing its rank there.

    Rows are the union of each strategy's top `limit` picks, ordered by the
    default (lineup) strategy's ranking first, with anyone another strategy
    would have taken instead appended below.
    """
    strategies = [s for s in ("lineup", "vona", "simulate") if s in results]
    ranks: dict[str, dict[str, tuple[int, Recommendation]]] = {}
    for strat in strategies:
        ranks[strat] = {
            r.player.player_id: (i + 1, r) for i, r in enumerate(results[strat])
        }

    order: list[str] = []
    seen: set[str] = set()
    primary = strategies[0]
    for r in results[primary][:limit]:
        if r.player.player_id not in seen:
            order.append(r.player.player_id)
            seen.add(r.player.player_id)
    for strat in strategies[1:]:
        for r in results[strat][:limit]:
            if r.player.player_id not in seen:
                order.append(r.player.player_id)
                seen.add(r.player.player_id)

    table = Table(header_style="bold", pad_edge=False, padding=(0, 1))
    table.add_column("Player", min_width=18, no_wrap=True)
    table.add_column("Pos", width=5)
    table.add_column("Proj", width=5, justify="right")
    for strat in strategies:
        table.add_column(STRATEGY_LABEL.get(strat, strat), width=10, justify="center")

    by_id: dict[str, Player] = {}
    for strat in strategies:
        for r in results[strat]:
            by_id.setdefault(r.player.player_id, r.player)

    for pid in order:
        p = by_id[pid]
        row = [Text(p.label, style="bold"), pos_text(f"{p.position}{p.pos_rank}"), f"{p.points:.0f}"]
        for strat in strategies:
            entry = ranks[strat].get(pid)
            if entry is None:
                row.append(Text("—", style="grey37"))
                continue
            rank, r = entry
            best = ranks[strat][results[strat][0].player.player_id][1].score
            cost = "best" if rank == 1 else fmt_score(r.score - best)
            row.append(Text(f"#{rank} {cost}", style="green" if rank == 1 else ""))
        table.add_row(*row)
    return table, strategies


def compare_verdict(results: dict[str, list[Recommendation]]) -> Text:
    strategies = [s for s in ("lineup", "vona", "simulate") if s in results and results[s]]
    tops = {s: results[s][0].player for s in strategies}
    names = {p.player_id: p for p in tops.values()}
    text = Text()
    if len(names) == 1:
        p = next(iter(names.values()))
        text.append("AGREE: ", style="bold green")
        text.append(f"every strategy takes {p.name} ({p.position}, {p.team or 'FA'})")
    else:
        text.append("SPLIT: ", style="bold yellow")
        text.append(
            ", ".join(f"{STRATEGY_LABEL.get(s, s)} wants {p.name}" for s, p in tops.items())
        )
        text.append("\n")
        text.append(
            "lineup backtests best on 2024-25 (see README); defaulting to it is the "
            "safer read, but a split like this is worth a second look.",
            style="dim",
        )
    return text


def agreement(results: dict[str, list[Recommendation]], primary: str) -> Text:
    """One line on whether the other strategies back the primary's top pick."""
    tops = {s: rows[0].player for s, rows in results.items() if rows}
    lead = tops.get(primary)
    others = [(s, p) for s, p in tops.items() if s != primary]
    if lead is None or not others:
        return Text()
    text = Text()
    if all(p.player_id == lead.player_id for _, p in others):
        text.append("AGREE: ", style="bold green")
        text.append(f"all {len(tops)} strategies take {lead.name}", style="dim")
        return text
    text.append("SPLIT: ", style="bold yellow")
    text.append(
        ", ".join(
            f"{STRATEGY_LABEL.get(s, s)} wants {p.name}"
            for s, p in others
            if p.player_id != lead.player_id
        ),
        style="dim",
    )
    text.append(
        f" · this screen ranks by {STRATEGY_LABEL.get(primary, primary).lower()}; "
        "a split is worth a second look",
        style="dim",
    )
    return text


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
    status: Text | None = None,
    alt: dict[str, list[Recommendation]] | None = None,
    primary: str = "lineup",
    pending: tuple[str, ...] = (),
    stale: tuple[str, ...] = (),
) -> RenderableType:
    """The full report. `status`, `alt`, `pending` and `stale` are what watch
    adds: a freshness line, the rival strategies' rankings alongside the primary
    one, and which of those columns is still being computed or is a pick old."""
    from rich.columns import Columns

    my_next = my_picks[0] if my_picks else None
    following = my_picks[1] if len(my_picks) > 1 else None
    current = my_next or state.current_pick
    alt = alt or {}
    columns = tuple(
        s for s in STRATEGY_ORDER if s != primary and (s in alt or s in pending)
    )
    fresh = {s: rows for s, rows in alt.items() if s not in stale}
    parts: list[RenderableType] = [header(league, state, my_next, on_clock)]
    if status is not None:
        parts.append(status)
    parts += [Text(), verdict(recs)]
    if len(fresh) > 1:
        parts.append(agreement(fresh, primary))
    parts += [
        recommendations_table(recs, width, alt, columns, stale),
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
            "turn) · Cost expected points behind the best pick"
            + (
                " · Vona/Simu that strategy's own rank for the same player"
                " (… still ranking, * a pick behind)"
                if columns
                else ""
            ),
            style="dim",
        ),
    ]
    return Group(*parts)
