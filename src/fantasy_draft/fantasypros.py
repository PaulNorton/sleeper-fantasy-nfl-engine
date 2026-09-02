"""Expert consensus rankings from FantasyPros.

Only the cheatsheet pages are read. They are ordinary server-rendered pages, not
under any path FantasyPros disallows in robots.txt, and the site's crawl delay
is honoured between requests. The full projections tables are deliberately not
taken: they load from /ajax/, which robots.txt disallows.

What the cheatsheet gives that Sleeper does not:
  - a consensus of 100+ experts rather than one projection
  - how much those experts disagree, which is the seed of the risk model
  - bye weeks
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

BASE = "https://www.fantasypros.com/nfl/rankings"
USER_AGENT = "fantasy-draft/0.1 (personal Sleeper draft assistant)"
CRAWL_DELAY = 5.0

# Cheatsheet slug per league format.
SLUGS = {
    "std": "consensus",
    "ppr": "ppr",
    "half_ppr": "half-point-ppr",
    "superflex": "superflex",
}

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


def cheatsheet_url(scoring_format: str, superflex: bool = False) -> str:
    slug = SLUGS["superflex"] if superflex else SLUGS.get(scoring_format, "consensus")
    return f"{BASE}/{slug}-cheatsheets.php"


def _fetch(url: str, cache_dir: Path, ttl: float) -> str:
    global _last_fetch
    cache = cache_dir / f"fp_{abs(hash(url)) % (10**12)}.html"
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
