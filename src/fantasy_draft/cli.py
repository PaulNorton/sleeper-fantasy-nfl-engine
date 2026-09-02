"""Command line interface."""

from __future__ import annotations

import time
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.table import Table
from rich.text import Text

from . import ui
from .league import LeagueSettings
from .recommend import recommend
from .session import build_session, load_config, save_config
from .simulate import RolloutConfig
from .sleeper import SleeperClient, SleeperError

app = typer.Typer(add_completion=False, help="Draft assistant for Sleeper leagues.")
console = Console()


def _opts(
    username: Optional[str],
    league_id: Optional[str],
    draft_id: Optional[str],
    season: Optional[str],
    slot: Optional[int],
    overrides: Optional[str] = None,
) -> dict:
    cfg = load_config()
    return {
        "username": username or cfg.get("username"),
        "user_id": cfg.get("user_id"),
        "league_id": league_id or cfg.get("league_id"),
        "draft_id": draft_id or cfg.get("draft_id"),
        "season": season or cfg.get("season"),
        "slot": slot or cfg.get("slot"),
        "overrides": overrides or cfg.get("overrides"),
    }


def _no_user(username: str) -> str:
    if "@" in username:
        return (
            f"{username!r} is an email address. Sleeper's API only accepts your public "
            "username handle - find it in the Sleeper app under Account, or in the URL "
            "of your league page."
        )
    return f"no Sleeper user named {username!r}"


U = typer.Option(None, "--username", "-u", help="Your Sleeper username.")
L = typer.Option(None, "--league-id", "-l", help="Sleeper league id.")
D = typer.Option(None, "--draft-id", "-d", help="Sleeper draft id (works for mocks too).")
S = typer.Option(None, "--season", help="Season, e.g. 2026.")
SL = typer.Option(None, "--slot", help="Your draft slot, if it cannot be detected.")
OV = typer.Option(None, "--projections", help="CSV of your own projections/ADP to overlay.")
SIM = typer.Option(300, "--sims", help="Draft simulations per candidate. More is steadier, slower.")
RISK = typer.Option(
    None, "--risk",
    help="Percentile of your simulated seasons to optimise: 0.25 protects the floor, "
         "0.75 chases upside. Default optimises the average.",
)
NOFP = typer.Option(False, "--no-consensus", help="Skip FantasyPros expert consensus.")
STRAT = typer.Option(
    "lineup", "--strategy",
    help="How to rank. 'lineup' scores what a player adds to your starting lineup and "
         "backtests best. 'vona' also subtracts what his position would still offer at "
         "your next pick. 'simulate' plays the rest of the draft out - backtests worst, "
         "kept for comparison and required by --objective win.",
)
OBJ = typer.Option(
    "points", "--objective",
    help="'points' maximises your expected total. 'win' maximises the chance of "
         "finishing top of the league, which is where a player's upside actually pays.",
)


@app.command()
def setup(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    slot: Optional[int] = SL,
    season: Optional[str] = S,
) -> None:
    """Save your league so other commands need no flags.

    Give a username, or a league id straight from your Sleeper league URL.
    With only a league id, add --slot so the app knows which team is yours.
    """
    if not username and not league_id:
        raise typer.BadParameter("give --username or --league-id")
    with SleeperClient() as client:
        season = season or str(client.state().get("season"))
        if not username:
            league = client.league(league_id)
            if not league:
                raise typer.BadParameter(f"no Sleeper league with id {league_id!r}")
            cfg = load_config()
            cfg.update(league_id=str(league_id), season=str(league.get("season") or season))
            if slot:
                cfg["slot"] = int(slot)
            save_config(cfg)
            console.print(f"[green]Saved.[/] {league.get('name')} ({cfg['season']})")
            if not slot:
                console.print("[yellow]No draft slot set — pass --slot to see your own roster.[/]")
            raise typer.Exit(0)
        user = client.user(username)
        if not user:
            raise typer.BadParameter(_no_user(username))
        leagues = client.user_leagues(str(user["user_id"]), season)
        if not league_id:
            if not leagues:
                console.print(f"[yellow]{username} has no {season} leagues.[/]")
                raise typer.Exit(1)
            if len(leagues) == 1:
                league_id = str(leagues[0]["league_id"])
            else:
                _print_leagues(leagues)
                console.print("\nRe-run with [bold]--league-id[/] to choose one.")
                raise typer.Exit(0)
        cfg = load_config()
        cfg.update(
            username=username,
            user_id=str(user["user_id"]),
            league_id=str(league_id),
            season=season,
        )
        if slot:
            cfg["slot"] = int(slot)
        save_config(cfg)
        name = next(
            (l.get("name") for l in leagues if str(l["league_id"]) == str(league_id)), league_id
        )
        console.print(f"[green]Saved.[/] {username} → {name} ({season})")


def _print_leagues(leagues: list[dict]) -> None:
    table = Table("League", "ID", "Teams", "Status")
    for lg in leagues:
        table.add_row(
            lg.get("name", "?"),
            str(lg.get("league_id")),
            str(lg.get("total_rosters")),
            str(lg.get("status")),
        )
    console.print(table)


@app.command()
def leagues(username: Optional[str] = U, season: Optional[str] = S) -> None:
    """List your Sleeper leagues for a season."""
    cfg = load_config()
    username = username or cfg.get("username")
    if not username:
        raise typer.BadParameter("pass --username or run `fdraft setup` first")
    with SleeperClient() as client:
        user = client.user(username)
        if not user:
            raise typer.BadParameter(_no_user(username))
        season = season or cfg.get("season") or str(client.state().get("season"))
        _print_leagues(client.user_leagues(str(user["user_id"]), season))


@app.command()
def board(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    draft_id: Optional[str] = D,
    season: Optional[str] = S,
    slot: Optional[int] = SL,
    projections: Optional[str] = OV,
    sims: int = SIM,
    risk: Optional[float] = RISK,
    no_consensus: bool = NOFP,
    strategy: str = STRAT,
    objective: str = OBJ,
    limit: int = typer.Option(12, "--limit", "-n", help="How many picks to show."),
) -> None:
    """Rank the board for your next pick."""
    with SleeperClient() as client:
        session = _session(client, username, league_id, draft_id, season, slot,
                           projections, not no_consensus)
        console.print(_screen(session, limit, sims, risk, objective, strategy))


@app.command()
def compare(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    draft_id: Optional[str] = D,
    season: Optional[str] = S,
    slot: Optional[int] = SL,
    projections: Optional[str] = OV,
    sims: int = SIM,
    no_consensus: bool = NOFP,
    limit: int = typer.Option(8, "--limit", "-n", help="How many picks to show per strategy."),
) -> None:
    """Rank the board under every strategy side by side, for a second opinion.

    lineup backtests clearly best over 2024-25 (see README) and is what board
    and watch act on. This is for checking whether the strategies agree at the
    moment of your actual pick, not for picking a different default.
    """
    with SleeperClient() as client:
        session = _session(client, username, league_id, draft_id, season, slot,
                           projections, not no_consensus)
        picks = session.my_picks(limit=8)
        results = {}
        for strat in ("lineup", "vona", "simulate"):
            results[strat] = recommend(
                session.league, session.state, session.players, session.replacement,
                session.my_roster_id, picks, limit=limit,
                cfg=RolloutConfig(sims=sims, strategy=strat),
            )
        table, strategies = ui.compare_table(results, limit)
        on_clock = session.on_clock()
        my_next = picks[0] if picks else None
        console.print(ui.header(session.league, session.state, my_next, on_clock))
        console.print()
        console.print(ui.compare_verdict(results))
        console.print()
        console.print(table)
        console.print(
            Text(
                "Each column shows rank and points behind that strategy's own top pick. "
                "Simulate is noisier than the other two; raise --sims if it looks unstable.",
                style="dim",
            )
        )


@app.command()
def watch(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    draft_id: Optional[str] = D,
    season: Optional[str] = S,
    slot: Optional[int] = SL,
    projections: Optional[str] = OV,
    sims: int = SIM,
    risk: Optional[float] = RISK,
    no_consensus: bool = NOFP,
    strategy: str = STRAT,
    objective: str = OBJ,
    interval: float = typer.Option(5.0, "--interval", "-i", help="Seconds between polls."),
    limit: int = typer.Option(12, "--limit", "-n", help="How many picks to show."),
) -> None:
    """Follow the draft live and re-rank after every pick."""
    with SleeperClient() as client:
        session = _session(client, username, league_id, draft_id, season, slot,
                           projections, not no_consensus)
        last = -1
        with Live(_screen(session, limit, sims, risk, objective, strategy), console=console, screen=True,
                  refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                try:
                    session.refresh_picks()
                except SleeperError:
                    continue
                if len(session.state.picks) != last:
                    last = len(session.state.picks)
                    live.update(_screen(session, limit, sims, risk, objective, strategy))
                if session.state.status == "complete":
                    break
        console.print("[green]Draft complete.[/]")


@app.command()
def roster(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    draft_id: Optional[str] = D,
    season: Optional[str] = S,
    slot: Optional[int] = SL,
    projections: Optional[str] = OV,
    no_consensus: bool = NOFP,
) -> None:
    """Show your drafted roster and which starter slots are still open."""
    with SleeperClient() as client:
        session = _session(client, username, league_id, draft_id, season, slot,
                           projections, not no_consensus)
        console.print(ui.roster_panel(session.league, session.my_players()))


@app.command()
def available(
    position: Optional[str] = typer.Option(None, "--pos", "-p", help="Filter by position."),
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    draft_id: Optional[str] = D,
    season: Optional[str] = S,
    slot: Optional[int] = SL,
    projections: Optional[str] = OV,
    no_consensus: bool = NOFP,
    limit: int = typer.Option(25, "--limit", "-n", help="How many players to list."),
) -> None:
    """List the best players still on the board, by raw value."""
    with SleeperClient() as client:
        session = _session(client, username, league_id, draft_id, season, slot,
                           projections, not no_consensus)
        pool = session.available()
        if position:
            pool = [p for p in pool if p.position == position.upper()]
        pool.sort(key=lambda p: -p.vorp)
        table = Table("Player", "Pos", "Tm", "Proj", "VOR", "Floor", "Ceil", "ADP", "Bye",
                      title="On the board")
        for p in pool[:limit]:
            table.add_row(
                p.label,
                f"{p.position}{p.pos_rank}",
                p.team or "FA",
                f"{p.points:.0f}",
                f"{p.vorp:+.0f}",
                f"{p.floor:.0f}",
                f"{p.ceiling:.0f}",
                "—" if p.adp >= 999 else f"{p.adp:.0f}",
                str(p.bye or "—"),
            )
        console.print(table)


@app.command()
def backtest(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    season: str = typer.Option("2025", "--season", help="A finished season to replay."),
    reps: int = typer.Option(3, "--reps", help="Repetitions per draft slot."),
    slots: Optional[str] = typer.Option(
        None, "--slots", help="Comma-separated draft slots to test. Default is all."
    ),
    sims: int = typer.Option(120, "--sims", help="Simulations per pick while drafting."),
    rounds: int = typer.Option(15, "--rounds", help="Rounds per draft."),
    opponent: str = typer.Option("adp", "--opponent", help="Strategy the other seats use."),
) -> None:
    """Replay a finished season to see how the engine would have drafted.

    Teams draft knowing only that season's preseason projections and ADP, then
    rosters are scored on what really happened. Every strategy faces identical
    drafts, so only the focal seat's choices differ.
    """
    from .backtest import prepare, report, run

    cfg = load_config()
    league_id = league_id or cfg.get("league_id")
    if not league_id:
        raise typer.BadParameter("need a league id to take the scoring rules from")
    chosen = [int(x) for x in slots.split(",")] if slots else None

    with SleeperClient() as client:
        league_data = client.league(league_id)
        if not league_data:
            raise typer.BadParameter(f"no Sleeper league with id {league_id!r}")
        league = LeagueSettings.from_api(league_data)
        console.print(f"[dim]{league.name}: {league.describe()}[/]")
        with console.status(f"loading {season} projections and results…"):
            players, replacement, actual = prepare(client, league, season)
        total = reps * len(chosen or range(league.teams)) * 4
        done = {"n": 0}
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
            TextColumn("{task.completed}/{task.total}"), console=console,
        ) as bar:
            task = bar.add_task("drafting", total=total)

            def tick(rep, slot, strategy):
                done["n"] += 1
                bar.update(task, completed=done["n"],
                           description=f"rep {rep + 1} slot {slot} {strategy}")

            results = run(
                league, players, replacement, actual, reps=reps, slots=chosen,
                rounds=rounds, cfg=RolloutConfig(sims=sims), opponent=opponent,
                progress=tick,
            )
    console.print()
    console.print(report(results, season))


def _session(client, username, league_id, draft_id, season, slot, overrides=None,
             consensus=True):
    opts = _opts(username, league_id, draft_id, season, slot, overrides)
    opts["consensus"] = consensus
    try:
        return build_session(client, **opts)
    except SleeperError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


def _screen(session, limit: int, sims: int = 300, risk: float | None = None,
            objective: str = "points", strategy: str = "vona"):
    # Beating the field can only be judged by simulating the other rosters.
    if objective == "win":
        strategy = "simulate"
    picks = session.my_picks(limit=8)
    recs = recommend(
        session.league,
        session.state,
        session.players,
        session.replacement,
        session.my_roster_id,
        picks,
        limit=limit,
        cfg=RolloutConfig(sims=sims, risk=risk, objective=objective, strategy=strategy),
    )
    if session.my_roster_id is None:
        console.print(
            Text(
                "Could not tell which team is yours — pass --slot to fix the roster view.",
                style="yellow",
            )
        )
    return ui.build_screen(
        session.league,
        session.state,
        recs,
        session.my_players(),
        picks,
        session.available(),
        session.by_id,
        session.roster_names,
        session.on_clock(),
        console.width,
    )


if __name__ == "__main__":
    app()
