"""Match a parsed :class:`MediaItem` against Trakt's catalogue."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import image_match
from .api import TraktClient, TraktError, TraktNotFoundError
from .parser import KIND_EPISODE, KIND_MOVIE, KIND_SHOW, MediaItem

_LOGGER = logging.getLogger(__name__)

#: how far a candidate's runtime may sit from the reported duration before we
#: stop believing it is the same thing (as a fraction of the reported duration)
_RUNTIME_TOLERANCE = 0.35

#: Runtime verdicts, ordered worst to best. A candidate we know nothing about
#: outranks one we have positively measured as the wrong length.
_FIT_MISMATCH = 0
_FIT_UNKNOWN = 1
_FIT_MATCH = 2

#: How many identically titled shows we are willing to check the artwork
#: against. Two is the normal case (an original and its revival); the cap stops
#: a generic title like "Life" from turning one lookup into a dozen.
_MAX_TWINS = 3

#: A zero-argument coroutine that yields the player's artwork hash. Passing the
#: loader rather than the hash keeps the image fetch off the common path: it
#: only runs if the title turns out to be ambiguous.
ThumbnailSource = int | Callable[[], Awaitable[int | None]] | None


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


def _fold(title: Any) -> str:
    """Normalise a title for comparison: case, accents and punctuation.

    Trakt spells the Shogun series "Shōgun"; players generally do not.
    """
    text = unicodedata.normalize("NFKD", str(title or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _runtime_fit(runtime_minutes: Any, duration_seconds: float | None) -> int:
    """Does a candidate's runtime agree with what the player reports?

    For a show Trakt's ``runtime`` is the average episode length, so it is
    comparable with the duration of the episode being played.
    """
    if not duration_seconds or not runtime_minutes:
        return _FIT_UNKNOWN
    try:
        runtime = float(runtime_minutes) * 60
    except (TypeError, ValueError):
        return _FIT_UNKNOWN
    if runtime <= 0:
        return _FIT_UNKNOWN
    drift = abs(runtime - duration_seconds) / duration_seconds
    return _FIT_MATCH if drift <= _RUNTIME_TOLERANCE else _FIT_MISMATCH


@dataclass(slots=True)
class _Candidate:
    """One Trakt search hit, with runtime kept as its own ranking key.

    Runtime cannot be folded into the score. Trakt returns relevance as a
    number around 1.16e18, where one float64 step is 256 -- so the obvious
    `score += bonus` is discarded outright (`score + 25.0 == score` is *True*).
    That is why the runtime tie-breaker never once fired.

    The two keys are weighted differently depending on what is being compared:

    * `rank` (within one search) puts relevance first, because Trakt's own
      ordering is authoritative there and a show's `runtime` is only an
      *average* -- Trakt lists Ted Lasso at 45 minutes though season one runs
      nearer 31, so a strict runtime test would demote the right show.
    * `kind_rank` (across the movie and show searches) puts runtime first and
      breaks ties on whether the title matches exactly, because the two scores
      come from separate queries, are not comparable, and in practice come back
      byte-identical. Without the title check a 65-minute Kamen Rider film
      whose name merely *contains* "Shogun" ties the Shōgun series on runtime
      and wins the coin toss.
    """

    body: dict[str, Any]
    fit: int
    score: float
    index: int
    exact: bool = False

    @property
    def rank(self) -> tuple[float, int, int]:
        # Ties go to the earlier hit: Trakt returns results most-relevant first.
        return (self.score, self.fit, -self.index)

    @property
    def kind_rank(self) -> tuple[int, bool, float]:
        return (self.fit, self.exact, self.score)


def _rank_all(
    results: list[dict[str, Any]],
    key: str,
    duration: float | None,
    query: str | None = None,
) -> list[_Candidate]:
    """Every candidate of one type in a Trakt search response, best first."""
    wanted = _fold(query) if query else ""
    candidates = [
        _Candidate(
            body=body,
            fit=_runtime_fit(body.get("runtime"), duration),
            score=float(entry.get("score") or 0),
            index=index,
            exact=bool(wanted) and _fold(body.get("title")) == wanted,
        )
        for index, entry in enumerate(results)
        if (body := entry.get(key))
    ]
    return sorted(candidates, key=lambda candidate: candidate.rank, reverse=True)


def _rank(
    results: list[dict[str, Any]],
    key: str,
    duration: float | None,
    query: str | None = None,
) -> _Candidate | None:
    """Pick the best candidate of one type out of a Trakt search response."""
    candidates = _rank_all(results, key, duration, query)
    return candidates[0] if candidates else None


def _twins(candidates: list[_Candidate]) -> list[_Candidate]:
    """The leading candidate plus anything else called exactly the same thing.

    Trakt hands back one identical relevance score for every hit in a response,
    so when two shows genuinely share a title there is nothing in the ranking
    to separate them and the winner is whichever Trakt happened to list first.
    That is how "The Grand Tour" resolves to the 2016 series rather than the
    2026 one. These are the candidates the artwork has to decide between.
    """
    if not candidates:
        return []
    wanted = _fold(candidates[0].body.get("title"))
    same = [
        candidate
        for candidate in candidates
        if _fold(candidate.body.get("title")) == wanted
    ]
    return same[:_MAX_TWINS]


def _show_id(show: dict[str, Any]) -> int | None:
    return (show.get("ids") or {}).get("trakt")


class _Thumbnail:
    """The player's artwork hash, fetched at most once and only when needed."""

    __slots__ = ("_hash", "_loaded", "_source")

    def __init__(self, source: ThumbnailSource = None) -> None:
        self._source = source
        self._hash: int | None = source if isinstance(source, int) else None
        self._loaded = source is None or isinstance(source, int)

    async def async_hash(self) -> int | None:
        if not self._loaded:
            self._loaded = True
            try:
                self._hash = await self._source()  # type: ignore[misc]
            except Exception as err:  # noqa: BLE001 - artwork is best-effort
                _LOGGER.debug("Could not load player artwork: %s", err)
                self._hash = None
        return self._hash


def _best(
    results: list[dict[str, Any]], key: str, duration: float | None
) -> dict[str, Any] | None:
    candidate = _rank(results, key, duration)
    return candidate.body if candidate is not None else None


class TraktResolver:
    """Resolves media items to Trakt ids, caching what it has already looked up."""

    def __init__(self, client: TraktClient, *, next_episode_fallback: bool) -> None:
        self._client = client
        self._next_episode_fallback = next_episode_fallback
        self._cache: dict[tuple, Resolved] = {}
        #: folded title -> the Trakt show id the artwork settled on, so a title
        #: with a twin is only disambiguated once per session.
        self._twin_choice: dict[tuple[str, int | None], int] = {}
        #: titles we have already complained about, so an unresolvable twin is
        #: reported once rather than on every session.
        self._warned_twins: set[str] = set()

    def set_next_episode_fallback(self, enabled: bool) -> None:
        self._next_episode_fallback = enabled

    def invalidate(self) -> None:
        self._cache.clear()
        self._twin_choice.clear()

    async def async_resolve(
        self,
        item: MediaItem,
        duration: float | None = None,
        thumbnail_hash: ThumbnailSource = None,
    ) -> Resolved:
        """Resolve ``item``, raising :class:`ResolutionError` when we cannot.

        ``thumbnail_hash`` is a difference hash of the player's current artwork,
        or a coroutine function returning one. For a show with no episode number
        it identifies the exact episode by matching Trakt's episode stills
        instead of guessing, and when two shows share a title it is also what
        decides which of them is playing.
        """
        key = (item.kind, item.title.lower(), item.year, item.season, item.episode)
        # Only explicit identities (a parsed S/E, or a movie) are cacheable.
        # Shows and bare titles resolve to an episode dynamically, so caching
        # them under the title would pin the wrong episode next time.
        # An episode with no season is not a complete identity: the same number
        # means a different episode once the show gains a second season, so it
        # must be re-resolved rather than pinned in the cache.
        cacheable = item.kind == KIND_MOVIE or (
            item.kind == KIND_EPISODE and item.season is not None
        )
        if cacheable and (cached := self._cache.get(key)) is not None:
            return cached

        try:
            resolved = await self._resolve(item, duration, _Thumbnail(thumbnail_hash))
        except TraktError as err:
            raise ResolutionError("trakt_error", str(err)) from err

        if cacheable and not resolved.guessed:
            self._cache[key] = resolved
        return resolved

    async def _resolve(
        self, item: MediaItem, duration: float | None, thumb: _Thumbnail
    ) -> Resolved:
        if item.kind == KIND_MOVIE:
            return await self._resolve_movie(item, duration)
        if item.kind == KIND_EPISODE:
            return await self._resolve_episode(item, duration, thumb)
        if item.kind == KIND_SHOW:
            return await self._resolve_show(item, duration, thumb)
        return await self._resolve_ambiguous(item, duration, thumb)

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

    async def _find_shows(
        self, title: str, year: int | None, duration: float | None = None
    ) -> list[dict[str, Any]]:
        """Shows Trakt knows by this name, best first.

        More than one comes back only when the title is genuinely shared, in
        which case the caller has to pick between them; the runtime ordering
        that normally settles it is meaningless between identical titles.
        """
        # A show's `runtime` is its average episode length, so comparing it with
        # the player's reported duration is a useful tie-breaker here too.
        results = await self._client.async_search("show", title, year)
        candidates = _twins(_rank_all(results, "show", duration, title))
        if not candidates:
            raise ResolutionError("show_not_found", f"No Trakt show for {title!r}")

        return self._prefer_pinned(title, year, [c.body for c in candidates])

    def _prefer_pinned(
        self, title: str, year: int | None, shows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Narrow same-named shows to the one already settled this session."""
        if len(shows) < 2:
            return shows
        pinned = self._twin_choice.get((_fold(title), year))
        for show in shows:
            if pinned is not None and _show_id(show) == pinned:
                return [show]
        _LOGGER.debug(
            "%r matches %d Trakt shows: %s",
            title,
            len(shows),
            ", ".join(f"{s.get('title')} ({s.get('year')})" for s in shows),
        )
        return shows

    def _warn_ambiguous(
        self, title: str, count: int, chosen: dict[str, Any], why: str
    ) -> None:
        """Say once that a title is shared and which show we settled for."""
        key = _fold(title)
        log = _LOGGER.warning if key not in self._warned_twins else _LOGGER.debug
        self._warned_twins.add(key)
        log(
            "%r matches %d Trakt shows and %s; assuming %s (%s)",
            title,
            count,
            why,
            chosen.get("title"),
            chosen.get("year"),
        )

    def _pin_twin(self, title: str, year: int | None, show: dict[str, Any]) -> None:
        """Remember which of the same-named shows the artwork picked."""
        show_id = _show_id(show)
        if show_id is not None:
            self._twin_choice[(_fold(title), year)] = show_id

    async def _resolve_episode(
        self,
        item: MediaItem,
        duration: float | None = None,
        thumb: _Thumbnail | None = None,
    ) -> Resolved:
        thumb = thumb or _Thumbnail()
        shows = await self._find_shows(item.title, item.year, duration)

        found: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for show in shows:
            show_id = _show_id(show)
            season = item.season
            if season is None:
                # The player gave an episode number but no season. That is only
                # unambiguous when the show has a single season, which is why
                # the season list decides it rather than an assumed 1.
                season = await self._sole_season(show_id)
                if season is None:
                    _LOGGER.debug(
                        "%s (%s) has more than one season, cannot place episode "
                        "%s alone",
                        show.get("title"),
                        show.get("year"),
                        item.episode,
                    )
                    continue
            try:
                episode = await self._client.async_get_episode(
                    show_id, season, item.episode or 0
                )
            except TraktNotFoundError:
                # A show that does not have this episode at all is not the one
                # being played -- which, with same-named shows, rules out most.
                _LOGGER.debug(
                    "%s (%s) has no S%02dE%02d",
                    show.get("title"),
                    show.get("year"),
                    season,
                    item.episode or 0,
                )
                continue
            found.append((show, episode))

        if not found:
            if item.season is None:
                # Nothing could place a bare episode number; fall back to
                # identifying the episode from the artwork instead. The shows
                # are already in hand, so don't search for them again.
                return await self._show_episode(item, shows, thumb)
            raise ResolutionError(
                "episode_not_found",
                f"{item.title} has no S{item.season:02d}E{item.episode or 0:02d}",
            )

        (show, episode), decided = await self._pick_episode(
            found, item, duration, thumb
        )
        # Only a real decision is worth remembering; pinning a coin toss would
        # cement it for the rest of the session.
        if decided and len(shows) > 1:
            self._pin_twin(item.title, item.year, show)
        return self._episode_result(show, episode, item.method)

    async def _pick_episode(
        self,
        found: list[tuple[dict[str, Any], dict[str, Any]]],
        item: MediaItem,
        duration: float | None,
        thumb: _Thumbnail,
    ) -> tuple[tuple[dict[str, Any], dict[str, Any]], bool]:
        """Choose between same-named shows that both carry this episode.

        The episode's own runtime is a far sharper test than the show average
        the search ranking had to use, so it goes first; the artwork settles
        whatever is left. The flag says whether anything actually decided, as
        opposed to the last candidate standing being taken on trust.
        """
        if len(found) == 1:
            return found[0], True

        fitting = [
            pair
            for pair in found
            if _runtime_fit(pair[1].get("runtime"), duration) == _FIT_MATCH
        ]
        if len(fitting) == 1:
            show = fitting[0][0]
            _LOGGER.debug(
                "Episode runtime picked %s (%s) out of %d same-named shows",
                show.get("title"),
                show.get("year"),
                len(found),
            )
            return fitting[0], True

        remaining = fitting or found
        if (picked := await self._match_episode_still(remaining, thumb)) is not None:
            return picked, True

        self._warn_ambiguous(
            item.title,
            len(found),
            remaining[0][0],
            "neither the episode runtime nor the artwork separates them",
        )
        return remaining[0], False

    async def _match_episode_still(
        self,
        pairs: list[tuple[dict[str, Any], dict[str, Any]]],
        thumb: _Thumbnail,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Compare the player's artwork with each show's still for this episode."""
        if not image_match.available():
            return None
        target = await thumb.async_hash()
        if target is None:
            return None

        best: tuple[int, tuple[dict[str, Any], dict[str, Any]]] | None = None
        for show, episode in pairs:
            shots = (episode.get("images") or {}).get("screenshot") or []
            if not shots:
                continue
            still = await image_match.async_fetch_and_hash(
                self._client.session, image_match.https(shots[0])
            )
            if still is None:
                continue
            distance = image_match.hamming(target, still)
            _LOGGER.debug(
                "Artwork is %d bits from %s (%s) S%02dE%02d",
                distance,
                show.get("title"),
                show.get("year"),
                episode.get("season") or 0,
                episode.get("number") or 0,
            )
            if distance <= image_match.MAX_DISTANCE and (
                best is None or distance < best[0]
            ):
                best = (distance, (show, episode))

        if best is None:
            return None
        show = best[1][0]
        _LOGGER.info(
            "Artwork identified %s (%s) out of %d same-named shows (distance %d)",
            show.get("title"),
            show.get("year"),
            len(pairs),
            best[0],
        )
        return best[1]

    async def _sole_season(self, show_id: int) -> int | None:
        """The show's only season number, or ``None`` if it has several."""
        try:
            seasons = await self._client.async_get_seasons(show_id)
        except TraktError as err:
            _LOGGER.debug("Could not list seasons for %s: %s", show_id, err)
            return None
        # Season 0 is Trakt's specials bucket and never the one we want.
        numbers = {
            number
            for season in seasons
            if isinstance(number := season.get("number"), int) and number > 0
        }
        return numbers.pop() if len(numbers) == 1 else None

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
            extra={
                "show_title": show.get("title"),
                "episode_title": episode.get("title"),
                "runtime": episode.get("runtime"),
            },
        )

    async def _resolve_show(
        self,
        item: MediaItem,
        duration: float | None,
        thumb: _Thumbnail | None = None,
    ) -> Resolved:
        """A show we can name but no episode number.

        Try to pin the exact episode by matching the player's thumbnail against
        Trakt's episode stills; only if that fails fall back to guessing the
        next unwatched episode.
        """
        thumb = thumb or _Thumbnail()
        shows = await self._find_shows(item.title, item.year, duration)
        return await self._show_episode(item, shows, thumb)

    async def _show_episode(
        self,
        item: MediaItem,
        shows: list[dict[str, Any]],
        thumb: _Thumbnail,
        artwork_tried: bool = False,
    ) -> Resolved:
        """Pin an episode of one of ``shows``, which all answer to the name.

        ``artwork_tried`` says the caller already ran the stills past the
        artwork and came back empty-handed, so doing it again would refetch
        every still of every candidate for the same answer.
        """
        if not artwork_tried and (
            matched := await self._match_across(item, shows, thumb)
        ):
            return matched

        show = shows[0]
        show_id = _show_id(show)
        if len(shows) > 1:
            self._warn_ambiguous(
                item.title, len(shows), show, "the artwork did not settle it"
            )

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

    async def _match_across(
        self, item: MediaItem, shows: list[dict[str, Any]], thumb: _Thumbnail
    ) -> Resolved | None:
        """Match the artwork against every show that answers to the name.

        With a single show this just identifies the episode. With two the
        episode it matches also says *which* show is playing: an Apple TV
        showing "The Grand Tour" is 46 episodes of the 2016 series and 6 of the
        2026 one, and only one of those 52 stills is the frame on screen.
        """
        if not image_match.available():
            return None
        target = await thumb.async_hash()
        if target is None:
            return None

        best: Resolved | None = None
        for show in shows:
            matched = await self._match_by_thumbnail(show, _show_id(show), target)
            if matched is None:
                continue
            if best is None or matched.extra.get(
                "thumbnail_distance", 0
            ) < best.extra.get("thumbnail_distance", 0):
                best = matched

        if best is None or len(shows) == 1:
            return best

        _LOGGER.info(
            "Artwork identified %s out of %d shows called %r",
            best.extra.get("show_title"),
            len(shows),
            item.title,
        )
        for show in shows:
            if _show_id(show) == best.show_id:
                self._pin_twin(item.title, item.year, show)
        return best

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

        _LOGGER.debug(
            "Thumbnail matching %s against %d episode stills",
            show.get("title"),
            len(candidates),
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
        thumb: _Thumbnail | None = None,
    ) -> Resolved:
        """A bare title. Ask Trakt whether it knows a film or a show by that name."""
        thumb = thumb or _Thumbnail()
        movies = await self._client.async_search("movie", item.title, item.year)
        shows = await self._client.async_search("show", item.title, item.year)

        best_movie = _rank(movies, "movie", duration, item.title)
        twin_candidates = _twins(_rank_all(shows, "show", duration, item.title))
        twins = self._prefer_pinned(
            item.title, item.year, [c.body for c in twin_candidates]
        )
        # Whatever the pin narrowed us to is the show the movie is weighed
        # against, so the two decisions cannot disagree.
        best_show = (
            next((c for c in twin_candidates if c.body is twins[0]), None)
            if twins
            else None
        )

        # A confirmed episode still is a near-certain signal — stronger than any
        # title score — so when the name matches a show, try the thumbnail before
        # deciding between film and series. "Silo" is both a film and a series;
        # the artwork settles it. It settles same-named *series* too, so every
        # candidate by that name gets checked, not just the top one.
        if twins and (matched := await self._match_across(item, twins, thumb)):
            return matched

        if best_movie is None and best_show is None:
            raise ResolutionError(
                "not_found", f"Trakt knows nothing called {item.title!r}"
            )

        if best_movie is None:
            prefer_show = True
        elif best_show is None:
            prefer_show = False
        else:
            # Runtime decides whenever it can tell the two apart. The two scores
            # come from separate searches and are usually identical anyway, so
            # comparing them alone is a coin toss that always lands on the film
            # -- which is how a 59-minute episode of "Dark Matter" ended up
            # scrobbled as the 90-minute 2008 film. On a genuine tie we still
            # keep the historical preference for the movie.
            prefer_show = best_show.kind_rank > best_movie.kind_rank

        if prefer_show:
            # The search that produced these candidates is the one to use --
            # re-running it under the winner's title and year would search
            # again for a show we are already holding.
            return await self._show_episode(item, twins, thumb, artwork_tried=True)

        return self._movie_result(best_movie.body, f"{item.method}->movie")
