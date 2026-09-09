"""Expert consensus rankings from FantasyPros.

Three rankings, one page shape. All of them are ordinary server-rendered pages
under /nfl/rankings/, which robots.txt allows, and the site's crawl delay is
honoured between requests. The full projections tables are deliberately not
taken: they load from /ajax/, which robots.txt disallows.

  - **draft** (`load_consensus`) - the cheatsheet, season-long, for the draft.
  - **weekly** (`load_weekly_consensus`) - this week only, for start/sit.
  - **rest of season** (`load_ros_consensus`) - for waivers and drops.

What any of them gives that Sleeper does not:
  - a consensus of 70+ experts rather than one projection
  - how much those experts disagree, which is the seed of the risk model
  - this week's opponent, and bye weeks

FantasyPros publishes weekly rankings for the upcoming week only; asking for a
later week returns an empty list, not last week's copy. That is why the weeks
beyond the next one fall back to the rest-of-season ranking - see
`waivers.load_horizon`.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

BASE = "https://www.fantasypros.com/nfl/rankings"
USER_AGENT = "sleeper-fantasy-nfl-engine/0.1 (personal Sleeper draft assistant)"
CRAWL_DELAY = 5.0

# Cheatsheet slug per league format.
SLUGS = {
    "std": "consensus",
    "ppr": "ppr",
    "half_ppr": "half-point-ppr",
    "superflex": "superflex",
}

# Weekly rankings. The flex page carries RB, WR and TE in one scoring-aware
# ranking; kickers and defences have their own pages and no scoring variant,
# since PPR does not touch them.
WEEKLY_FLEX_SLUGS = {"std": "flex", "ppr": "ppr-flex", "half_ppr": "half-point-ppr-flex"}

# Quarterbacks come off the superflex page, filtered to QB, not off `qb.php`.
# `qb.php` serves `var ecrData = []` to a plain HTTP client and fills the table
# in from a script this app does not run; the superflex page renders the same
# weekly quarterback ranking server-side. Only the QB rows are kept, so its
# standard-scoring receivers never displace the flex page's.
WEEKLY_OTHER_SLUGS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("superflex", ("QB",)),
    ("k", None),
    ("dst", None),
)

# Rest-of-season rankings. One page covers every position.
ROS_SLUGS = {"std": "ros-overall", "ppr": "ros-ppr-overall",
             "half_ppr": "ros-half-point-ppr-overall"}

# No superflex variant is fetched for either. Both rankings are consumed one
# position at a time - `projections.blend_ranks` re-seats each position on its
# own points curve - so the only thing a superflex list would change is where
# quarterbacks sit relative to everyone else, which is never read.

_ECR_RE = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\});", re.S)

# FantasyPros names some positions differently from Sleeper.
POSITION_ALIAS = {"DST": "DEF", "PK": "K"}
_last_fetch = 0.0


@dataclass
class Consensus:
    name: str
    position: str
    team: str | None
    ecr: float          # average expert rank
    rank_std: float     # how much the experts disagree, in rank units
    rank_min: float
    rank_max: float
    tier: int
    bye: int | None
    opponent: str | None = None   # weekly pages only, e.g. "at IND"


def cheatsheet_url(scoring_format: str, superflex: bool = False) -> str:
    slug = SLUGS["superflex"] if superflex else SLUGS.get(scoring_format, "consensus")
    return f"{BASE}/{slug}-cheatsheets.php"


def _fetch(url: str, cache_dir: Path, ttl: float) -> str:
    global _last_fetch
    # Digest, not hash(): Python salts string hashing per process, so a hash()
    # key names a different file every run and the cache never hits.
    cache = cache_dir / f"fp_{hashlib.sha1(url.encode()).hexdigest()[:16]}.html"
    if cache.exists() and time.time() - cache.stat().st_mtime < ttl:
        return cache.read_text()
    wait = CRAWL_DELAY - (time.time() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0,
                         follow_redirects=True)
        _last_fetch = time.time()
        resp.raise_for_status()
    except httpx.HTTPError:
        if cache.exists():
            return cache.read_text()
        raise
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(resp.text)
    return resp.text


def _num(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def parse(page: str) -> list[Consensus]:
    match = _ECR_RE.search(page)
    if not match:
        return []
    rows = json.loads(match.group(1)).get("players") or []
    out: list[Consensus] = []
    for row in rows:
        name = html_lib.unescape(str(row.get("player_name") or "")).strip()
        if not name:
            continue
        ecr = _num(row.get("rank_ave")) or _num(row.get("rank_ecr"))
        bye = row.get("player_bye_week")
        out.append(
            Consensus(
                name=name,
                position=POSITION_ALIAS.get(
                    str(row.get("player_position_id") or "").upper(),
                    str(row.get("player_position_id") or "").upper(),
                ),
                team=(str(row.get("player_team_id")).strip() or None),
                ecr=ecr,
                rank_std=_num(row.get("rank_std")),
                rank_min=_num(row.get("rank_min"), ecr),
                rank_max=_num(row.get("rank_max"), ecr),
                tier=int(_num(row.get("tier"), 0)),
                bye=int(_num(bye)) if str(bye).strip().isdigit() else None,
                opponent=(str(row.get("player_opponent")).strip() or None)
                if row.get("player_opponent") else None,
            )
        )
    return out


def load_consensus(
    scoring_format: str,
    superflex: bool,
    cache_dir: Path,
    ttl: float = 21600,
) -> list[Consensus]:
    return parse(_fetch(cheatsheet_url(scoring_format, superflex), cache_dir, ttl))


def _merge(pages: list[list[Consensus]]) -> list[Consensus]:
    """One list from several pages, first page winning a duplicate.

    The flex page is read before the single-position ones, so a player who
    appears on both keeps the ranking that was scored for this league.
    """
    seen: dict[tuple[str, str], Consensus] = {}
    for rows in pages:
        for row in rows:
            seen.setdefault((row.name.lower(), row.position), row)
    return list(seen.values())


def weekly_pages(scoring_format: str) -> list[tuple[str, tuple[str, ...] | None]]:
    """Each weekly page to read, and which positions to keep from it."""
    flex = WEEKLY_FLEX_SLUGS.get(scoring_format, "flex")
    return [(f"{BASE}/{flex}.php", None)] + [
        (f"{BASE}/{slug}.php", keep) for slug, keep in WEEKLY_OTHER_SLUGS
    ]


def ros_url(scoring_format: str) -> str:
    return f"{BASE}/{ROS_SLUGS.get(scoring_format, 'ros-overall')}.php"


def load_weekly_consensus(
    scoring_format: str, cache_dir: Path, ttl: float = 3600
) -> list[Consensus]:
    """This week's start/sit consensus, every position the app values.

    Short TTL by default: a weekly ranking moves with every practice report,
    and a stale one is exactly the injury news the engine has no other way of
    seeing. The season cheatsheet can sit for six hours; this cannot.
    """
    pages = []
    for url, keep in weekly_pages(scoring_format):
        rows = parse(_fetch(url, cache_dir, ttl))
        pages.append([r for r in rows if keep is None or r.position in keep])
    return _merge(pages)


def load_ros_consensus(
    scoring_format: str, cache_dir: Path, ttl: float = 21600
) -> list[Consensus]:
    """The experts' rest-of-season order, every position on one page."""
    return parse(_fetch(ros_url(scoring_format), cache_dir, ttl))
