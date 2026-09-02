"""Turn media_player attributes into something Trakt can identify.

Media players describe what they are playing very differently. Plex and Jellyfin
fill in the dedicated ``media_series_title`` / ``media_season`` /
``media_episode`` attributes, while an Apple TV only ever reports a
``media_title`` string whose shape depends on the app that is playing:

    Infuse       "Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio"
    Apple TV app "Ted Lasso"

Several apps put the show name in ``media_title`` and the episode reference in
``media_artist`` instead, which looks like a bare show name unless that second
field is read too:

    Prime Video  "The Boys"    + "Stagione 5, Ep. 6 Cadesse il cielo"
    Prime Video  "The Boys"    + "S5 E4 In aggiornamento"
    Disney+      "Only Murders" + "S1:E4 Alluvione lampo"

So we try the structured attributes first, then the title, then the companion
fields, and only give up on the episode number when none of them carry one.
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

# Punctuation apps put between the season and the episode, and between the
# episode number and its title.
_REF_SEP = r"[,;:·•∙\.\-–—]"

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


# Companion attributes that may carry the episode reference when `media_title`
# holds only the show name. Ordered by how likely they are to be right.
_EPISODE_REF_ATTRS: tuple[str, ...] = (
    "media_artist",
    "media_album_name",
    "media_album_artist",
)

# An episode number with no season beside it -- "Chernobyl Ep.05". Common for
# miniseries, where the app drops a season it considers redundant. The keyword
# is mandatory and must follow a separator, so "Sleep 2024" cannot match it.
_EPISODE_ONLY_RE = re.compile(
    r"^(?P<show>.+?)[\s:,\-–—]+"
    r"(?:episodio|episode|épisode|folge|ep)\b\s*\.?\s*"
    r"(?P<episode>\d{1,4})"
    rf"(?:\s*(?:{_DASH}|:)\s*(?P<title>.+))?$",
    re.IGNORECASE,
)

# A season/episode reference standing on its own, i.e. with no show name in
# front of it -- "Stagione 5, Ep. 6 Cadesse il cielo", "S5 E4 In aggiornamento",
# "S1:E4 Alluvione lampo", "Season 2, Episode 10: Title". The season word is
# required, so a plain artist or channel name cannot match by accident.
_EPISODE_REF_RE = re.compile(
    r"^\s*(?:stagione|season|staffel|saison|temporada|serie|s)\s*"
    r"(?P<season>\d{1,3})"
    rf"\s*{_REF_SEP}?\s*"
    r"(?:episodio|episode|épisode|folge|ep|e)\s*\.?\s*"
    r"(?P<episode>\d{1,4})"
    rf"(?:\s*{_REF_SEP}?\s*(?P<title>.+))?$",
    re.IGNORECASE,
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
        if self.kind == KIND_EPISODE and self.episode is not None:
            if self.season is None:
                # Season still to be settled against Trakt's season list.
                return f"{self.title} E{self.episode:02d}"
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


def parse_episode_ref(raw: str) -> tuple[int, int, str | None] | None:
    """Read a standalone ``S1:E4 Title`` style reference.

    Returns ``(season, episode, episode_title)``, or ``None`` when the string
    carries no season/episode -- which is what a music artist or a YouTube
    channel name in the same attribute does.
    """
    match = _EPISODE_REF_RE.match(raw.strip())
    if not match:
        return None
    season = _as_int(match.group("season"))
    episode = _as_int(match.group("episode"))
    if season is None or episode is None:
        return None
    title = match.group("title")
    return season, episode, (_clean(title) or None if title else None)


def parse_episode_only(raw: str) -> MediaItem | None:
    """Read a ``Show Ep.5`` title, where no season is given.

    The season is left as ``None`` for the resolver to settle: a miniseries has
    only one, so the number alone identifies the episode, but a show with
    several seasons stays ambiguous and must be pinned some other way.
    """
    match = _EPISODE_ONLY_RE.match(_clean(raw))
    if not match:
        return None
    episode = _as_int(match.group("episode"))
    if episode is None:
        return None
    show, year = _split_year(_clean(match.group("show")))
    if not show:
        return None
    episode_title = match.group("title")
    return MediaItem(
        kind=KIND_EPISODE,
        title=show,
        year=year,
        season=None,
        episode=episode,
        episode_title=_clean(episode_title) or None if episode_title else None,
        raw_title=raw,
        method="title_episode_only",
    )


def _episode_from_companion(
    attributes: dict[str, Any], show: str, year: int | None, raw_title: str
) -> MediaItem | None:
    """Look for the episode reference in the fields beside ``media_title``."""
    for attribute in _EPISODE_REF_ATTRS:
        value = attributes.get(attribute)
        if not value:
            continue
        reference = parse_episode_ref(str(value))
        if reference is None:
            continue
        season, episode, episode_title = reference
        return MediaItem(
            kind=KIND_EPISODE,
            title=show,
            year=year,
            season=season,
            episode=episode,
            episode_title=episode_title,
            raw_title=raw_title,
            method=f"companion_{attribute}",
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

    # 1b. Season and episode numbers, but no series title to hang them on. The
    #     title is then the show's, not the episode's.
    if not series:
        season = _as_int(attributes.get("media_season"))
        episode = _as_int(attributes.get("media_episode"))
        if season is not None and episode is not None:
            show, year = _split_year(_clean(raw_title))
            if show:
                return ParseResult(
                    MediaItem(
                        kind=KIND_EPISODE,
                        title=show,
                        year=year,
                        season=season,
                        episode=episode,
                        raw_title=raw_title,
                        method="attributes_no_series",
                    )
                )

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

    title, year = _split_year(_clean(raw_title))
    if not title:
        return ParseResult(reason="no_title")

    # 4. The title is a bare show name, but a companion attribute may still
    #    carry the episode. Prime Video and Disney+ both do this, and without
    #    reading it the resolver has nothing to go on but a guess.
    if (item := _episode_from_companion(attributes, title, year, raw_title)) is not None:
        return ParseResult(item)

    # 5. An episode number with no season, e.g. NOW's "Chernobyl Ep.05". Tried
    #    after the companion fields, which give a complete reference when they
    #    give one at all.
    if (item := parse_episode_only(raw_title)) is not None:
        return ParseResult(item)

    # 6. A bare name. It is a show or a film, but we cannot tell which from the
    #    string alone -- the Trakt lookup gets to decide.

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
