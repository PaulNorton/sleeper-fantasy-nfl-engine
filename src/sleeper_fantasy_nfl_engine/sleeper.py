"""Sleeper API client with an on-disk cache.

Sleeper's read API needs no authentication. Two hosts are in play:
  api.sleeper.app/v1  - documented league, draft and user endpoints
  api.sleeper.com     - projections endpoint (undocumented but stable)
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

V1 = "https://api.sleeper.app/v1"
V2 = "https://api.sleeper.com"

DEFAULT_CACHE = Path.home() / ".cache" / "sleeper-fantasy-nfl-engine"


class SleeperError(RuntimeError):
    pass


class SleeperClient:
    def __init__(self, cache_dir: Path | None = None, timeout: float = 20.0) -> None:
        self.cache_dir = cache_dir or DEFAULT_CACHE
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            timeout=timeout, headers={"User-Agent": "sleeper-fantasy-nfl-engine/0.1"}
        )
        # Freshness of the last network response, for the watch screen to show.
        self.last_fetch_at: float = 0.0
        self.last_edge_age: int = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SleeperClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- plumbing ----------

    def _cache_path(self, url: str, params: Any) -> Path:
        key = hashlib.sha256(f"{url}|{params!r}".encode()).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def _get(self, url: str, params: Any = None, ttl: float = 0.0) -> Any:
        """GET with optional cache. On network failure, fall back to stale cache."""
        path = self._cache_path(url, params)
        if ttl > 0 and path.exists() and time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text())
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            if path.exists():
                return json.loads(path.read_text())
            raise SleeperError(f"request failed: {url}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            if path.exists():
                return json.loads(path.read_text())
            raise SleeperError(f"HTTP {resp.status_code} from {url}")
        self.last_fetch_at = time.time()
        self.last_edge_age = int(resp.headers.get("age") or 0)
        data = resp.json()
        if ttl > 0:
            path.write_text(json.dumps(data))
        return data

    def _live(self, url: str) -> Any:
        """GET past Sleeper's CDN, for data that must reflect the last pick.

        Sleeper serves these endpoints with `public, s-maxage=..,
        stale-while-revalidate=300`, so Cloudflare answers the first request
        after a pick with the copy from before it and only then refreshes in the
        background. That reads as the board flicking between behind and current.
        Cloudflare ignores a client `Cache-Control: no-cache` here, but it keys
        on the full URL, so a unique parameter reaches the origin. The API
        ignores unknown parameters.
        """
        return self._get(url, params=[("_", str(time.time_ns()))], ttl=0)

    # ---------- endpoints ----------

    def state(self) -> dict:
        return self._get(f"{V1}/state/nfl", ttl=3600) or {}

    def user(self, username: str) -> dict | None:
        return self._get(f"{V1}/user/{username}", ttl=86400)

    def user_leagues(self, user_id: str, season: str) -> list[dict]:
        return self._get(f"{V1}/user/{user_id}/leagues/nfl/{season}", ttl=600) or []

    def league(self, league_id: str) -> dict | None:
        return self._get(f"{V1}/league/{league_id}", ttl=600)

    def league_users(self, league_id: str) -> list[dict]:
        return self._get(f"{V1}/league/{league_id}/users", ttl=600) or []

    def league_rosters(self, league_id: str) -> list[dict]:
        return self._get(f"{V1}/league/{league_id}/rosters", ttl=300) or []

    def league_drafts(self, league_id: str) -> list[dict]:
        return self._get(f"{V1}/league/{league_id}/drafts", ttl=60) or []

    def draft(self, draft_id: str) -> dict | None:
        return self._live(f"{V1}/draft/{draft_id}")

    def draft_picks(self, draft_id: str) -> list[dict]:
        return self._live(f"{V1}/draft/{draft_id}/picks") or []

    def traded_picks(self, draft_id: str) -> list[dict]:
        return self._get(f"{V1}/draft/{draft_id}/traded_picks", ttl=60) or []

    def projections(
        self, season: str, position: str, week: int | None = None, ttl: float | None = None
    ) -> list[dict]:
        """Season-long projections, or one week's when `week` is given.

        A week's projection moves through the week as practice reports land, so
        it gets a much shorter default TTL than the season number, which barely
        moves once the season starts.
        """
        params = [
            ("season_type", "regular"),
            ("position[]", position),
            ("order_by", "pts_ppr"),
        ]
        url = f"{V2}/projections/nfl/{season}"
        if week is not None:
            url += f"/{week}"
        if ttl is None:
            ttl = 3600 if week is not None else 21600
        return self._get(url, params=params, ttl=ttl) or []

    def schedule(self, season: str, ttl: float = 604800) -> list[dict]:
        """Every regular season game. Used to work out bye weeks."""
        return self._get(f"{V2}/schedule/nfl/regular/{season}", ttl=ttl) or []

    def stats(self, season: str, position: str, ttl: float = 604800) -> list[dict]:
        """Actual season stat lines. Finished seasons never change, so cache hard."""
        params = [
            ("season_type", "regular"),
            ("position[]", position),
            ("order_by", "pts_ppr"),
        ]
        return self._get(f"{V2}/stats/nfl/{season}", params=params, ttl=ttl) or []
