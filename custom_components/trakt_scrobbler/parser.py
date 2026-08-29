"""Turn media_player attributes into something Trakt can identify.

Media players describe what they are playing very differently. Plex and Jellyfin
fill in the dedicated ``media_series_title`` / ``media_season`` /
``media_episode`` attributes, while an Apple TV only ever reports a
``media_title`` string whose shape depends on the app that is playing:

    Infuse       "Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio"
    Apple TV app "Ted Lasso"

So we try the structured attributes first and only fall back to picking the
title apart with regexes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# "Kinds" of parse result.
KIND_MOVIE = "movie"
KIND_EPISODE = "episode"
KIND_SHOW = "show"  # a show we could name but not pin to an episode

# Separators used between "Show", "S1 ∙ E1" and "Episode title". Infuse uses a
# bullet operator (U+2219); other players use middots, bullets or plain dots.
_DASH = r"[-–—]"
_SE_SEP = r"[\s∙·•\.]"

_YEAR_RE = re.compile(r"\s*[\(\[](?P<year>(?:19|20)\d{2})[\)\]]\s*$")

# Junk that release-named files drag along; stripped before matching.
_NOISE_RE = re.compile(
    r"""\s*(?:
        [\(\[]\s*(?:4k|uhd|hdr|hdr10|dolby\s*vision|dv|sdr|imax|remux|
                    \d{3,4}p|x26[45]|hevc|h\.?26[45]|aac|ac3|eac3|dts(?:-hd)?|
                    truehd|atmos|web-?dl|webrip|bluray|bdrip|hdtv|proper|repack)
        [^\)\]]*[\)\]]
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)

_EPISODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio"  (Infuse)
    # "Show - S01 · E02"                               (no episode title)
    re.compile(
        rf"^(?P<show>.+?)\s*{_DASH}\s*S(?P<season>\d{{1,3}})\s*{_SE_SEP}\s*"
        rf"E(?P<episode>\d{{1,4}})(?:\s*{_DASH}\s*(?P<title>.+))?$",
        re.IGNORECASE,
    ),
    # "Show - S01E02 - Episode title"
    re.compile(
        rf"^(?P<show>.+?)\s*{_DASH}\s*S(?P<season>\d{{1,3}})\s*"
        rf"E(?P<episode>\d{{1,4}})(?:\s*{_DASH}\s*(?P<title>.+))?$",
        re.IGNORECASE,
    ),
    # "Show S01E02 Episode title" / "Show.S01E02.Episode.title"
    re.compile(
        rf"^(?P<show>.+?)[\s\._]+S(?P<season>\d{{1,3}})\s*E(?P<episode>\d{{1,4}})"
        rf"(?:[\s\._]*{_DASH}?[\s\._]*(?P<title>.+))?$",
        re.IGNORECASE,
    ),
    # "Show 1x02 - Episode title"
    re.compile(
        rf"^(?P<show>.+?)[\s\._]+(?P<season>\d{{1,2}})x(?P<episode>\d{{1,3}})"
        rf"(?:\s*{_DASH}\s*(?P<title>.+))?$",
        re.IGNORECASE,
    ),
    # "Show: Season 1: Episode 2: Episode title"  /  "Show - Season 1, Episode 2"
    re.compile(
        rf"^(?P<show>.+?)\s*[:\-–—,]\s*(?:season|stagione|staffel|saison|temporada)"
        rf"\s*(?P<season>\d{{1,3}})\s*[:\-–—,]\s*"
        rf"(?:episode|episodio|folge|épisode|ep\.?)\s*(?P<episode>\d{{1,4}})"
        rf"(?:\s*[:\-–—,]\s*(?P<title>.+))?$",
        re.IGNORECASE,
    ),
)


@dataclass(slots=True)
class MediaItem:
    """A normalised description of what a player is showing."""

    kind: str
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    raw_title: str | None = None
    #: which rule produced this result, surfaced for troubleshooting
    method: str = "unknown"

    @property
    def slug(self) -> str:
        """A short human label, e.g. ``Spider-Noir S01E01``."""
        if self.kind == KIND_EPISODE:
            return f"{self.title} S{self.season:02d}E{self.episode:02d}"
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title


@dataclass(slots=True)
class ParseResult:
    """Outcome of inspecting a player's attributes."""

    item: MediaItem | None = None
    #: set when nothing scrobbleable could be derived
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _clean(value: str) -> str:
    """Strip release noise and tidy separators."""
    text = _NOISE_RE.sub(" ", value)
    text = text.replace("_", " ")
    # Release-style dot separators, but keep "S.W.A.T." style initialisms.
    if "." in text and " " not in text:
        text = re.sub(r"(?<=\w{2})\.(?=\w)", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -–—:.·∙•")


def _split_year(value: str) -> tuple[str, int | None]:
    """Pull a trailing ``(2024)`` off a title."""
    match = _YEAR_RE.search(value)
    if not match:
        return value.strip(), None
    return value[: match.start()].strip(), int(match.group("year"))


def _as_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_title(raw: str) -> MediaItem | None:
    """Parse a free-form ``media_title`` into a movie or an episode.

    Returns ``None`` when the string carries no season/episode information and
    no year, i.e. when it is just a bare name that could be either.
    """
    text = _clean(raw)
    if not text:
        return None

    for index, pattern in enumerate(_EPISODE_PATTERNS):
        match = pattern.match(text)
        if not match:
            continue
        season = _as_int(match.group("season"))
        episode = _as_int(match.group("episode"))
        if season is None or episode is None:
            continue
        show, year = _split_year(_clean(match.group("show")))
        if not show:
            continue
        episode_title = match.groupdict().get("title")
        return MediaItem(
            kind=KIND_EPISODE,
            title=show,
            year=year,
            season=season,
            episode=episode,
            episode_title=_clean(episode_title) or None if episode_title else None,
            raw_title=raw,
            method=f"title_regex_{index}",
        )

    title, year = _split_year(text)
    if year and title:
        return MediaItem(
            kind=KIND_MOVIE,
            title=title,
            year=year,
            raw_title=raw,
            method="title_with_year",
        )
    return None


def parse_attributes(attributes: dict[str, Any]) -> ParseResult:
    """Derive a :class:`MediaItem` from a media_player's attributes."""
    raw_title = attributes.get("media_title")
    series = attributes.get("media_series_title")
    content_type = str(attributes.get("media_content_type") or "").lower()

    # 1. Players that fill in the structured episode attributes (Plex, Jellyfin,
    #    Emby, Kodi...). Trust them over any string parsing.
    if series:
        season = _as_int(attributes.get("media_season"))
        episode = _as_int(attributes.get("media_episode"))
        if season is not None and episode is not None:
            show, year = _split_year(_clean(str(series)))
            return ParseResult(
                MediaItem(
                    kind=KIND_EPISODE,
                    title=show,
                    year=year,
                    season=season,
                    episode=episode,
                    episode_title=_clean(str(raw_title)) if raw_title else None,
                    raw_title=raw_title,
                    method="attributes",
                )
            )

    if not raw_title:
        return ParseResult(reason="no_title")

    raw_title = str(raw_title)

    # 2. The player told us outright that this is a film.
    if content_type == "movie":
        title, year = _split_year(_clean(raw_title))
        if title:
            return ParseResult(
                MediaItem(
                    kind=KIND_MOVIE,
                    title=title,
                    year=year or _as_int(attributes.get("media_year")),
                    raw_title=raw_title,
                    method="content_type_movie",
                )
            )

    # 3. Pick the title string apart.
    if (item := parse_title(raw_title)) is not None:
        return ParseResult(item)

    # 4. A bare name. It is a show or a film, but we cannot tell which from the
    #    string alone -- the Trakt lookup gets to decide.
    title, year = _split_year(_clean(raw_title))
    if not title:
        return ParseResult(reason="no_title")

    if content_type in ("tvshow", "episode", "series", "season"):
        return ParseResult(
            MediaItem(
                kind=KIND_SHOW,
                title=title,
                year=year,
                raw_title=raw_title,
                method="content_type_show",
            )
        )

    return ParseResult(
        MediaItem(
            kind=KIND_SHOW if series else "ambiguous",
            title=title,
            year=year,
            raw_title=raw_title,
            method="bare_title",
        )
    )
