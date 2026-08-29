"""Match a parsed :class:`MediaItem` against Trakt's catalogue."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import image_match
from .api import TraktClient, TraktError, TraktNotFoundError
from .parser import KIND_EPISODE, KIND_MOVIE, KIND_SHOW, MediaItem

_LOGGER = logging.getLogger(__name__)

#: how far a candidate's runtime may sit from the reported duration before we
#: stop believing it is the same thing (as a fraction of the reported duration)
_RUNTIME_TOLERANCE = 0.35


@dataclass(slots=True)
class Resolved:
    """A Trakt-identified item, ready to be scrobbled."""

    kind: str  # "movie" or "episode"
    payload: dict[str, Any]
    display: str
    trakt_id: int | None = None
    show_id: int | None = None
    slug: str | None = None
    season: int | None = None
    episode: int | None = None
    method: str = ""
    #: True when we guessed the episode rather than reading it off the player
    guessed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str | None:
        if not self.slug:
            return None
        if self.kind == "movie":
            return f"https://trakt.tv/movies/{self.slug}"
        if self.season is not None and self.episode is not None:
            return (
                f"https://trakt.tv/shows/{self.slug}"
                f"/seasons/{self.season}/episodes/{self.episode}"
            )
        return f"https://trakt.tv/shows/{self.slug}"


class ResolutionError(Exception):
    """We could not decide what the player is showing."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def _runtime_penalty(runtime_minutes: Any, duration_seconds: float | None) -> float:
    """Score adjustment based on how well a candidate's runtime fits.

    Trakt's own relevance score is the primary signal; runtime is a tie-breaker
    that stops "Ted Lasso" the film from beating "Ted Lasso" the show.
    """
    if not duration_seconds or not runtime_minutes:
        return 0.0
    try:
        runtime = float(runtime_minutes) * 60
    except (TypeError, ValueError):
        return 0.0
    if runtime <= 0:
        return 0.0
    drift = abs(runtime - duration_seconds) / duration_seconds
    if drift <= _RUNTIME_TOLERANCE:
        return 25.0 * (1 - drift / _RUNTIME_TOLERANCE)
    return -20.0


def _best(
    results: list[dict[str, Any]], key: str, duration: float | None
) -> dict[str, Any] | None:
    """Pick the best candidate of one type out of a Trakt search response."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in results:
        body = entry.get(key)
        if not body:
            continue
        score = float(entry.get("score") or 0)
        score += _runtime_penalty(body.get("runtime"), duration)
        scored.append((score, body))
    if not scored:
        return None
    return max(scored, key=lambda pair: pair[0])[1]


def _best_scored(
    results: list[dict[str, Any]], key: str, duration: float | None
) -> tuple[float, dict[str, Any]] | None:
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in results:
        body = entry.get(key)
        if not body:
            continue
        score = float(entry.get("score") or 0)
        score += _runtime_penalty(body.get("runtime"), duration)
        scored.append((score, body))
    if not scored:
        return None
    return max(scored, key=lambda pair: pair[0])


class TraktResolver:
    """Resolves media items to Trakt ids, caching what it has already looked up."""

    def __init__(self, client: TraktClient, *, next_episode_fallback: bool) -> None:
        self._client = client
        self._next_episode_fallback = next_episode_fallback
        self._cache: dict[tuple, Resolved] = {}

    def set_next_episode_fallback(self, enabled: bool) -> None:
        self._next_episode_fallback = enabled

    def invalidate(self) -> None:
        self._cache.clear()

    async def async_resolve(
        self,
        item: MediaItem,
        duration: float | None = None,
        thumbnail_hash: int | None = None,
    ) -> Resolved:
        """Resolve ``item``, raising :class:`ResolutionError` when we cannot.

        ``thumbnail_hash`` is a difference hash of the player's current artwork;
        for a show with no episode number it lets us identify the exact episode
        by matching Trakt's episode stills instead of guessing.
        """
        key = (item.kind, item.title.lower(), item.year, item.season, item.episode)
        # Only explicit identities (a parsed S/E, or a movie) are cacheable.
        # Shows and bare titles resolve to an episode dynamically, so caching
        # them under the title would pin the wrong episode next time.
        cacheable = item.kind in (KIND_MOVIE, KIND_EPISODE)
        if cacheable and (cached := self._cache.get(key)) is not None:
            return cached

        try:
            resolved = await self._resolve(item, duration, thumbnail_hash)
        except TraktError as err:
            raise ResolutionError("trakt_error", str(err)) from err

        if cacheable and not resolved.guessed:
            self._cache[key] = resolved
        return resolved

    async def _resolve(
        self, item: MediaItem, duration: float | None, thumbnail_hash: int | None
    ) -> Resolved:
        if item.kind == KIND_MOVIE:
            return await self._resolve_movie(item, duration)
        if item.kind == KIND_EPISODE:
            return await self._resolve_episode(item, duration)
        if item.kind == KIND_SHOW:
            return await self._resolve_show(item, duration, thumbnail_hash)
        return await self._resolve_ambiguous(item, duration, thumbnail_hash)

    async def _resolve_movie(self, item: MediaItem, duration: float | None) -> Resolved:
        results = await self._client.async_search("movie", item.title, item.year)
        movie = _best(results, "movie", duration)
        if movie is None:
            raise ResolutionError("movie_not_found", f"No Trakt movie for {item.title!r}")
        return self._movie_result(movie, item.method)

    def _movie_result(self, movie: dict[str, Any], method: str) -> Resolved:
        ids = movie.get("ids", {})
        title = movie.get("title", "?")
        year = movie.get("year")
        return Resolved(
            kind="movie",
            payload={"movie": {"ids": {"trakt": ids.get("trakt")}}},
            display=f"{title} ({year})" if year else title,
            trakt_id=ids.get("trakt"),
            slug=ids.get("slug"),
            method=method,
        )

    async def _find_show(
        self, title: str, year: int | None, duration: float | None = None
    ) -> dict[str, Any]:
        # A show's `runtime` is its average episode length, so comparing it with
        # the player's reported duration is a useful tie-breaker here too.
        results = await self._client.async_search("show", title, year)
        show = _best(results, "show", duration)
        if show is None:
            raise ResolutionError("show_not_found", f"No Trakt show for {title!r}")
        return show

    async def _resolve_episode(
        self, item: MediaItem, duration: float | None = None
    ) -> Resolved:
        show = await self._find_show(item.title, item.year, duration)
        show_ids = show.get("ids", {})
        show_id = show_ids.get("trakt")

        try:
            episode = await self._client.async_get_episode(
                show_id, item.season or 0, item.episode or 0
            )
        except TraktNotFoundError as err:
            raise ResolutionError(
                "episode_not_found",
                f"{show.get('title')} has no S{item.season:02d}E{item.episode:02d}",
            ) from err

        return self._episode_result(show, episode, item.method)

    def _episode_result(
        self,
        show: dict[str, Any],
        episode: dict[str, Any],
        method: str,
        guessed: bool = False,
    ) -> Resolved:
        show_ids = show.get("ids", {})
        season = episode.get("season")
        number = episode.get("number")
        label = f"{show.get('title')} S{season:02d}E{number:02d}"
        if episode.get("title"):
            label = f"{label} – {episode['title']}"
        return Resolved(
            kind="episode",
            payload={
                "show": {"ids": {"trakt": show_ids.get("trakt")}},
                "episode": {"season": season, "number": number},
            },
            display=label,
            trakt_id=episode.get("ids", {}).get("trakt"),
            show_id=show_ids.get("trakt"),
            slug=show_ids.get("slug"),
            season=season,
            episode=number,
            method=method,
            guessed=guessed,
            extra={"episode_title": episode.get("title"), "runtime": episode.get("runtime")},
        )

    async def _resolve_show(
        self,
        item: MediaItem,
        duration: float | None,
        thumbnail_hash: int | None = None,
    ) -> Resolved:
        """A show we can name but no episode number.

        Try to pin the exact episode by matching the player's thumbnail against
        Trakt's episode stills; only if that fails fall back to guessing the
        next unwatched episode.
        """
        show = await self._find_show(item.title, item.year, duration)
        show_ids = show.get("ids", {})
        show_id = show_ids.get("trakt")

        if thumbnail_hash is not None:
            matched = await self._match_by_thumbnail(show, show_id, thumbnail_hash)
            if matched is not None:
                return matched

        if not self._next_episode_fallback:
            raise ResolutionError(
                "no_episode_number",
                f"{item.title!r} has no season/episode and the next-episode "
                "fallback is disabled",
            )

        season, number = 1, 1
        try:
            progress = await self._client.async_get_watched_progress(show_id)
        except TraktError as err:
            _LOGGER.debug("Could not read watched progress for %s: %s", show_id, err)
        else:
            if nxt := progress.get("next_episode"):
                season = nxt.get("season", 1)
                number = nxt.get("number", 1)

        try:
            episode = await self._client.async_get_episode(show_id, season, number)
        except TraktNotFoundError as err:
            raise ResolutionError(
                "episode_not_found",
                f"Guessed {show.get('title')} S{season:02d}E{number:02d} does not exist",
            ) from err

        return self._episode_result(show, episode, "next_episode_guess", guessed=True)

    async def _match_by_thumbnail(
        self, show: dict[str, Any], show_id: int, thumbnail_hash: int
    ) -> Resolved | None:
        """Identify the episode by matching the player artwork to Trakt stills."""
        if not image_match.available():
            return None

        try:
            seasons = await self._client.async_get_seasons(show_id)
        except TraktError as err:
            _LOGGER.debug("Could not list seasons for %s: %s", show_id, err)
            return None

        candidates: list[dict[str, Any]] = []
        episodes_by_key: dict[tuple[int, int], dict[str, Any]] = {}
        for season in seasons:
            number = season.get("number")
            if not number:  # skip specials (season 0)
                continue
            try:
                episodes = await self._client.async_get_season_episodes(show_id, number)
            except TraktError:
                continue
            for episode in episodes:
                shots = (episode.get("images") or {}).get("screenshot") or []
                if not shots:
                    continue
                s, n = episode.get("season"), episode.get("number")
                episodes_by_key[(s, n)] = episode
                candidates.append(
                    {"season": s, "episode": n, "title": episode.get("title"), "image": shots[0]}
                )

        match = await image_match.async_best_match(
            self._client.session, thumbnail_hash, candidates
        )
        if match is None:
            return None

        episode = episodes_by_key.get((match.season, match.episode))
        if episode is None:
            return None

        _LOGGER.info(
            "Thumbnail identified %s S%02dE%02d (distance %d, runner-up %s)",
            show.get("title"),
            match.season,
            match.episode,
            match.distance,
            match.runner_up,
        )
        resolved = self._episode_result(show, episode, "thumbnail_match")
        resolved.extra["thumbnail_distance"] = match.distance
        return resolved

    async def _resolve_ambiguous(
        self,
        item: MediaItem,
        duration: float | None,
        thumbnail_hash: int | None = None,
    ) -> Resolved:
        """A bare title. Ask Trakt whether it knows a film or a show by that name."""
        movies = await self._client.async_search("movie", item.title, item.year)
        shows = await self._client.async_search("show", item.title, item.year)

        best_movie = _best_scored(movies, "movie", duration)
        best_show = _best_scored(shows, "show", duration)

        # A confirmed episode still is a near-certain signal — stronger than any
        # title score — so when the name matches a show, try the thumbnail before
        # deciding between film and series. "Silo" is both a film and a series;
        # the artwork settles it.
        if thumbnail_hash is not None and best_show:
            show = best_show[1]
            matched = await self._match_by_thumbnail(
                show, show.get("ids", {}).get("trakt"), thumbnail_hash
            )
            if matched is not None:
                return matched

        if best_movie and (not best_show or best_movie[0] >= best_show[0]):
            return self._movie_result(best_movie[1], f"{item.method}->movie")

        if best_show:
            show_item = MediaItem(
                kind=KIND_SHOW,
                title=best_show[1].get("title", item.title),
                year=best_show[1].get("year"),
                raw_title=item.raw_title,
                method=item.method,
            )
            return await self._resolve_show(show_item, duration, thumbnail_hash)

        raise ResolutionError("not_found", f"Trakt knows nothing called {item.title!r}")
