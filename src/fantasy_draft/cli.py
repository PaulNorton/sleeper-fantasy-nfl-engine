"""Command line interface."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.table import Table
from rich.text import Text

from . import ui
from .changes import find_movers, load_snapshot, save_snapshot
from .league import LeagueSettings
from .lineup import find_swaps
from .projections import load_week_points
from .recommend import recommend
from .season import build_season_session
from .session import build_session, load_config, save_config
from .simulate import RolloutConfig
from .sleeper import SleeperClient, SleeperError
from .valuation import LineupEvaluator
from .waivers import rank_adds, rank_drops

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
RID = typer.Option(None, "--roster-id", help="Your roster id, if it cannot be detected.")
WK = typer.Option(None, "--week", help="Week to plan for. Default is Sleeper's current week.")


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
    interval: float = typer.Option(10.0, "--interval", "-i", help="Seconds between polls."),
    limit: int = typer.Option(12, "--limit", "-n", help="How many picks to show."),
) -> None:
    """Follow the draft live and re-rank after every pick.

    Every pick is ranked under all three strategies, so a disagreement shows up
    while there is still time to think about it. The closed-form two rank in
    milliseconds and paint at once; simulate takes seconds, so it runs on a
    worker thread and its column fills in behind, marked until it lands.

    The line under the header says when the board was last read, whether it came
    straight from Sleeper, and how current the simulate column is.
    """
    primary, fast = _watch_strategies(strategy, objective)

    with SleeperClient() as client:
        session = _session(client, username, league_id, draft_id, season, slot,
                           projections, not no_consensus)
        poll: dict = {"error": None}
        results: dict = {}
        sim: dict = {"future": None, "at": None, "ranked_at": None,
                     "started": None, "error": None}
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="simulate")

        def start_simulate() -> None:
            """Hand simulate a frozen copy of the board and let it run behind."""
            if sim["future"] is not None:
                # Only bites if it has not started; stops picks arriving faster
                # than a rollout from queueing a backlog of dead work.
                sim["future"].cancel()
            snapshot = replace(session.state, picks=list(session.state.picks))
            sim["at"] = len(snapshot.picks)
            sim["started"] = time.time()
            sim["future"] = pool.submit(
                _rank, session, snapshot, session.my_roster_id, "simulate",
                limit, sims, risk, objective,
            )

        def collect(block: bool = False) -> None:
            future: Future | None = sim["future"]
            if future is None or not (block or future.done()):
                return
            sim["future"] = None
            sim["started"] = None
            try:
                results["simulate"] = future.result()
                sim["ranked_at"] = sim["at"]
                sim["error"] = None
            except Exception as exc:  # a failed rollout must not kill the watch
                sim["error"] = str(exc)

        def rank_board() -> None:
            """Paint from the closed-form strategies; simulate catches up after."""
            for strat in fast:
                results[strat] = _rank(session, session.state, session.my_roster_id,
                                       strat, limit, sims, risk, objective)
            start_simulate()
            if primary == "simulate":
                # Nothing to draw without it, so this one wait is unavoidable.
                collect(block=True)

        def render() -> object:
            return _watch_screen(session, results, primary, limit, interval,
                                 poll["error"], sim)

        rank_board()
        last = len(session.state.picks)
        polled = time.time()
        try:
            with Live(render(), console=console, screen=True, refresh_per_second=4) as live:
                while True:
                    if time.time() - polled >= interval:
                        polled = time.time()
                        try:
                            session.refresh_picks()
                            poll["error"] = None
                        except SleeperError as exc:
                            # Keep the last good board; the status line says why.
                            poll["error"] = str(exc)
                        else:
                            if len(session.state.picks) != last:
                                last = len(session.state.picks)
                                rank_board()
                    collect()
                    live.update(render())
                    if session.state.status == "complete" and sim["future"] is None:
                        break
                    # Tick faster than the poll so the clocks in the status line
                    # stay honest while simulate is still running.
                    time.sleep(1.0)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
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
def lineup(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    season: Optional[str] = S,
    roster_id: Optional[int] = RID,
    week: Optional[int] = WK,
    projections: Optional[str] = OV,
    no_consensus: bool = NOFP,
) -> None:
    """Compare your saved lineup to the optimal one for a week."""
    with SleeperClient() as client:
        sess = _season_session(client, username, league_id, season, roster_id,
                               projections, not no_consensus)
        row = sess.my_roster()
        if row is None:
            console.print("[yellow]Could not tell which team is yours — pass --roster-id.[/]")
            raise typer.Exit(1)
        wk = week or int(client.state().get("display_week") or client.state().get("week") or 1)

        rostered_ids = {str(pid) for pid in (row.get("players") or [])}
        week_points = {p.player_id: p for p in load_week_points(client, sess.league, sess.season, wk)}
        roster = [week_points[pid] for pid in rostered_ids if pid in week_points]
        # Anyone the weekly feed has nothing for (a bye, an untracked backup)
        # cannot be recommended blind, so he is seated at zero rather than left
        # out - leaving him out would let the optimiser ignore that he holds a
        # slot at all.
        for pid in rostered_ids - set(week_points):
            base = sess.players.get(pid)
            if base is not None:
                roster.append(replace(base, points=0.0))

        swaps = find_swaps(sess.league, roster, sess.my_starter_ids())
        if not swaps:
            console.print(f"[green]Your week {wk} lineup already matches the optimiser.[/]")
            raise typer.Exit(0)
        table = Table("Start", "Pos", "Proj", "Over", "Proj", title=f"Week {wk} lineup changes")
        for s in swaps:
            table.add_row(
                s.start.label, s.start.position, f"{s.start.points:.1f}",
                s.sit.label if s.sit else "(empty slot)",
                f"{s.sit.points:.1f}" if s.sit else "—",
            )
        console.print(table)


@app.command()
def waivers(
    position: Optional[str] = typer.Option(None, "--pos", "-p", help="Filter by position."),
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    season: Optional[str] = S,
    roster_id: Optional[int] = RID,
    projections: Optional[str] = OV,
    no_consensus: bool = NOFP,
    limit: int = typer.Option(20, "--limit", "-n", help="How many free agents to list."),
) -> None:
    """Rank free agents by what they would add to your roster, not their raw projection."""
    with SleeperClient() as client:
        sess = _season_session(client, username, league_id, season, roster_id,
                               projections, not no_consensus)
        if sess.my_roster_id is None:
            console.print("[yellow]Could not tell which team is yours — pass --roster-id.[/]")
            raise typer.Exit(1)
        evaluator = LineupEvaluator.from_league(sess.league, sess.replacement)
        pool = sess.free_agents()
        if position:
            pool = [p for p in pool if p.position == position.upper()]
        targets = rank_adds(evaluator, sess.my_players(), pool, limit=limit)
        table = Table("Player", "Pos", "Tm", "Proj", "Adds", "ADP", title="Free agents")
        for t in targets:
            table.add_row(
                t.player.label,
                f"{t.player.position}{t.player.pos_rank}",
                t.player.team or "FA",
                f"{t.player.points:.0f}",
                f"{t.added_value:+.1f}",
                "—" if t.player.adp >= 999 else f"{t.player.adp:.0f}",
            )
        console.print(table)


@app.command()
def drops(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    season: Optional[str] = S,
    roster_id: Optional[int] = RID,
    projections: Optional[str] = OV,
    no_consensus: bool = NOFP,
    limit: int = typer.Option(10, "--limit", "-n", help="How many roster spots to list."),
) -> None:
    """Rank your own roster by how little each player is contributing — safest drops first."""
    with SleeperClient() as client:
        sess = _season_session(client, username, league_id, season, roster_id,
                               projections, not no_consensus)
        if sess.my_roster_id is None:
            console.print("[yellow]Could not tell which team is yours — pass --roster-id.[/]")
            raise typer.Exit(1)
        evaluator = LineupEvaluator.from_league(sess.league, sess.replacement)
        candidates = rank_drops(evaluator, sess.my_players(), limit=limit)
        table = Table("Player", "Pos", "Tm", "Proj", "Contribution", title="Safest drops first")
        for c in candidates:
            table.add_row(
                c.player.label,
                f"{c.player.position}{c.player.pos_rank}",
                c.player.team or "FA",
                f"{c.player.points:.0f}",
                f"{c.contribution:+.1f}",
            )
        console.print(table)


@app.command()
def alerts(
    username: Optional[str] = U,
    league_id: Optional[str] = L,
    season: Optional[str] = S,
    projections: Optional[str] = OV,
    no_consensus: bool = NOFP,
) -> None:
    """Flag players whose season projection moved a lot since the last run.

    There is no news feed behind this - it notices an injury or a depth-chart
    change the same way, indirectly, as a projection that jumped. Silent on
    the first run for a league, since there is nothing yet to compare against.
    """
    with SleeperClient() as client:
        sess = _season_session(client, username, league_id, season, None,
                               projections, not no_consensus)
        previous = load_snapshot(client.cache_dir, sess.league.league_id, sess.season)
        players = list(sess.players.values())
        movers = find_movers(players, previous)
        save_snapshot(client.cache_dir, sess.league.league_id, sess.season, players)
        if not previous:
            console.print("[dim]First run for this league — nothing to compare to yet.[/]")
            raise typer.Exit(0)
        if not movers:
            console.print("[green]No projection has moved enough to flag.[/]")
            raise typer.Exit(0)
        table = Table("Player", "Pos", "Tm", "Was", "Now", "Change", title="Projection movers")
        for m in movers:
            table.add_row(
                m.player.label,
                f"{m.player.position}{m.player.pos_rank}",
                m.player.team or "FA",
                f"{m.previous:.0f}",
                f"{m.current:.0f}",
                f"{m.delta:+.0f}",
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


def _season_session(client, username, league_id, season, roster_id, overrides=None,
                    consensus=True):
    cfg = load_config()
    try:
        return build_season_session(
            client,
            username=username or cfg.get("username"),
            user_id=cfg.get("user_id"),
            league_id=league_id or cfg.get("league_id"),
            season=season or cfg.get("season"),
            roster_id=roster_id,
            overrides=overrides or cfg.get("overrides"),
            consensus=consensus,
        )
    except SleeperError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


def _watch_strategies(strategy: str, objective: str) -> tuple[str, list[str]]:
    """Which strategy the board is ordered by, and which rank fast enough to
    paint at once.

    The primary must be one the board can be drawn from, so it is either in the
    fast list or it is simulate, which the caller waits on. Under --objective
    win only simulate can measure a share of the title odds, so the closed-form
    two are not run at all.
    """
    if objective == "win":
        return "simulate", []
    return strategy, ["lineup", "vona"]


def _rank(session, state, roster_id, strategy: str, limit: int, sims: int,
          risk: float | None, objective: str) -> list:
    """Rank one strategy against an explicit draft state.

    The state and roster id are passed in rather than read off the session so
    that simulate can run on a worker thread against a snapshot, while the main
    loop keeps polling and rebinding the session's own state.
    """
    picks = state.my_upcoming_picks(roster_id, limit=8) if roster_id is not None else []
    return recommend(
        session.league, state, session.players, session.replacement,
        roster_id, picks, limit=limit,
        cfg=RolloutConfig(sims=sims, risk=risk, objective=objective, strategy=strategy),
    )


def _watch_screen(session, results: dict, primary: str, limit: int, interval: float,
                  error: str | None, sim: dict):
    picks = session.my_picks(limit=8)
    made = len(session.state.picks)
    status = ui.freshness(
        made,
        session.fetched_at,
        session.changed_at,
        edge_age=session.edge_age,
        interval=interval,
        error=error,
    )
    if primary != "simulate":
        status.append_text(
            ui.simulate_note(made, sim["ranked_at"], sim["started"], sim["error"])
        )
    stale = ()
    if "simulate" in results and sim["ranked_at"] != made:
        stale = ("simulate",)
    # A failed rollout with nothing to show drops its column: the red status
    # line carries the error, where an ellipsis would imply work still running.
    failed_cold = sim["error"] and "simulate" not in results
    pending = ("simulate",) if primary != "simulate" and not failed_cold else ()
    return ui.build_screen(
        session.league,
        session.state,
        results[primary],
        session.my_players(),
        picks,
        session.available(),
        session.by_id,
        session.roster_names,
        session.on_clock(),
        console.width,
        status=status,
        alt=results,
        primary=primary,
        pending=pending,
        stale=stale,
    )


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
