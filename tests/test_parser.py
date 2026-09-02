"""Parser tests, built from titles this integration actually sees in the wild.

Runs standalone (``python tests/test_parser.py``) as well as under pytest, so it
can be executed inside the Home Assistant container without extra dependencies.
"""

from __future__ import annotations

import importlib.util
import os
import sys

# Load parser.py straight off disk: importing the package would drag in
# `homeassistant`, which these tests deliberately do not need.
_PARSER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "custom_components",
    "trakt_scrobbler",
    "parser.py",
)
_spec = importlib.util.spec_from_file_location("trakt_parser", _PARSER_PATH)
parser = importlib.util.module_from_spec(_spec)
# `dataclasses` looks the defining module up in sys.modules, so register it first.
sys.modules["trakt_parser"] = parser
_spec.loader.exec_module(parser)

KIND_EPISODE = parser.KIND_EPISODE
KIND_MOVIE = parser.KIND_MOVIE
KIND_SHOW = parser.KIND_SHOW
parse_attributes = parser.parse_attributes
parse_title = parser.parse_title

# (title, kind, show/movie title, season, episode)
EPISODE_CASES = [
    # Infuse on an Apple TV -- the bullet-operator form.
    ("Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio", "Spider-Noir", 1, 1),
    ("The Bear - S03 ∙ E07 - Legacy", "The Bear", 3, 7),
    ("Show - S01 · E02", "Show", 1, 2),
    ("Show - S1 • E2 - Title", "Show", 1, 2),
    # Compact forms.
    ("Severance - S02E05 - Trojan's Horse", "Severance", 2, 5),
    ("Severance S02E05", "Severance", 2, 5),
    ("Silo.S01E03.Machines", "Silo", 1, 3),
    ("Andor 2x04 - Ever Been to Ghorman?", "Andor", 2, 4),
    # Spelled out, including a couple of localisations.
    ("Fallout: Season 1: Episode 3: The Head", "Fallout", 1, 3),
    ("Dark - Stagione 2, Episodio 4", "Dark", 2, 4),
    # Release-style noise should not derail the season/episode match.
    ("Shogun - S01E02 - Servants of Two Masters [1080p]", "Shogun", 1, 2),
]

MOVIE_CASES = [
    ("Dune: Part Two (2024)", "Dune: Part Two", 2024),
    ("Blade Runner 2049 (2017)", "Blade Runner 2049", 2017),
    ("The Matrix [1999]", "The Matrix", 1999),
]

# Titles that carry no season/episode and no year -- the parser must not invent one.
BARE_CASES = ["Ted Lasso", "Interstellar", "Slow Horses"]


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_episode_titles() -> None:
    for raw, show, season, episode in EPISODE_CASES:
        item = parse_title(raw)
        _check(item is not None, f"{raw!r} did not parse")
        _check(item.kind == KIND_EPISODE, f"{raw!r} -> {item.kind}, expected episode")
        _check(item.title == show, f"{raw!r} -> show {item.title!r}, expected {show!r}")
        _check(
            item.season == season and item.episode == episode,
            f"{raw!r} -> S{item.season}E{item.episode}, expected S{season}E{episode}",
        )


def test_movie_titles() -> None:
    for raw, title, year in MOVIE_CASES:
        item = parse_title(raw)
        _check(item is not None, f"{raw!r} did not parse")
        _check(item.kind == KIND_MOVIE, f"{raw!r} -> {item.kind}, expected movie")
        _check(item.title == title, f"{raw!r} -> {item.title!r}, expected {title!r}")
        _check(item.year == year, f"{raw!r} -> year {item.year}, expected {year}")


def test_bare_titles_are_not_guessed() -> None:
    for raw in BARE_CASES:
        _check(parse_title(raw) is None, f"{raw!r} should not parse to a concrete item")


def test_apple_tv_infuse_attributes() -> None:
    """The exact attribute payload an Apple TV reports while Infuse is playing."""
    result = parse_attributes(
        {
            "media_content_type": "video",
            "media_duration": 2815,
            "media_title": "Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio",
            "app_id": "com.firecore.infuse",
            "app_name": "Infuse",
        }
    )
    _check(result.item is not None, "Infuse payload did not parse")
    _check(result.item.kind == KIND_EPISODE, "Infuse payload should be an episode")
    _check(result.item.title == "Spider-Noir", f"got {result.item.title!r}")
    _check(result.item.season == 1 and result.item.episode == 1, "wrong S/E")
    _check(
        result.item.episode_title == "Entra nel mio ufficio",
        f"got {result.item.episode_title!r}",
    )


def test_apple_tv_app_attributes() -> None:
    """The Apple TV app reports only a series name -- flagged for the Trakt lookup."""
    result = parse_attributes(
        {
            "media_content_id": "A0006504004",
            "media_content_type": "video",
            "media_duration": 2819,
            "media_title": "Ted Lasso",
            "app_id": "com.apple.TVWatchList",
            "app_name": "TV",
        }
    )
    _check(result.item is not None, "Apple TV payload did not parse")
    _check(
        result.item.kind == "ambiguous",
        f"expected ambiguous, got {result.item.kind}",
    )
    _check(result.item.title == "Ted Lasso", f"got {result.item.title!r}")


def test_structured_attributes_win() -> None:
    """Plex-style attributes are trusted over the title string."""
    result = parse_attributes(
        {
            "media_content_type": "tvshow",
            "media_series_title": "The Expanse",
            "media_season": "4",
            "media_episode": "6",
            "media_title": "Displacement",
            "media_duration": 3000,
        }
    )
    _check(result.item is not None, "structured payload did not parse")
    _check(result.item.kind == KIND_EPISODE, "should be an episode")
    _check(result.item.title == "The Expanse", f"got {result.item.title!r}")
    _check(result.item.season == 4 and result.item.episode == 6, "wrong S/E")
    _check(result.item.method == "attributes", f"got {result.item.method}")
    _check(result.item.episode_title == "Displacement", "lost the episode title")


def test_content_type_movie() -> None:
    result = parse_attributes(
        {"media_content_type": "movie", "media_title": "Arrival", "media_duration": 6960}
    )
    _check(result.item.kind == KIND_MOVIE, "should be a movie")
    _check(result.item.title == "Arrival", f"got {result.item.title!r}")


def test_show_content_type_without_numbers() -> None:
    result = parse_attributes(
        {"media_content_type": "tvshow", "media_title": "Slow Horses"}
    )
    _check(result.item.kind == KIND_SHOW, f"got {result.item.kind}")


def test_no_title() -> None:
    _check(parse_attributes({}).item is None, "empty attributes should not parse")
    _check(parse_attributes({}).reason == "no_title", "expected a no_title reason")


# Apps that put the show in media_title and the episode in a companion field.
# (attributes, show, season, episode, episode_title)
COMPANION_CASES = [
    # Prime Video on an Apple TV, Italian UI.
    (
        {"media_title": "Reacher", "media_artist": "Stagione 4, Ep. 6 Lo sfortunato Plum"},
        "Reacher", 4, 6, "Lo sfortunato Plum",
    ),
    # The same app sometimes uses the short form instead.
    (
        {"media_title": "La Fattoria Clarkson", "media_artist": "S5 E4 In aggiornamento"},
        "La Fattoria Clarkson", 5, 4, "In aggiornamento",
    ),
    # Disney+.
    (
        {"media_title": "Furious", "media_artist": "S1:E4 Alluvione lampo"},
        "Furious", 1, 4, "Alluvione lampo",
    ),
    # English and other locales.
    (
        {"media_title": "The Boys", "media_artist": "Season 5, Ep. 6 Fall of the Sky"},
        "The Boys", 5, 6, "Fall of the Sky",
    ),
    (
        {"media_title": "Dark", "media_artist": "Staffel 3, Folge 7 Der Titel"},
        "Dark", 3, 7, "Der Titel",
    ),
    (
        {"media_title": "Show", "media_album_name": "Season 2, Episode 10: The Title"},
        "Show", 2, 10, "The Title",
    ),
]

# A companion field holding something that is not an episode reference at all --
# a YouTube channel, a band, a broadcaster. These must fall through untouched.
COMPANION_NON_EPISODES = [
    "Reed's Smart Home",
    "Real Civil Engineer",
    "Elio e le Storie Tese",
    "Corridor Crew e KreatureKid",
    "Aldo Giovanni e Giacomo Ufficiale",
    "The Grand Tour",
    "Sky Uno",
]

# "Show Ep.N": an episode number with no season beside it.
EPISODE_ONLY_CASES = [
    ("Chernobyl Ep.05", "Chernobyl", 5),
    ("Chernobyl - Ep. 5", "Chernobyl", 5),
    ("Chernobyl Episodio 3", "Chernobyl", 3),
    ("Band of Brothers Ep 7", "Band of Brothers", 7),
    ("Die Welle Folge 4", "Die Welle", 4),
]

# Titles that merely contain the letters "ep" and must not be mistaken for one.
EPISODE_ONLY_NON_MATCHES = [
    "Sleep 2024",
    "The Sleepover 2",
    "Deep 6",
    "Steep 3",
    "Prep 101",
    "Jeep 4x4 Adventure",
    "Pechino Express",
    "Episodes",
    "Chernobyl",
    "Chernobyl: I nastri perduti",
]


def test_companion_field_carries_the_episode() -> None:
    for attributes, show, season, episode, episode_title in COMPANION_CASES:
        item = parse_attributes(attributes).item
        _check(item is not None, f"{attributes!r} did not parse")
        _check(
            item.kind == KIND_EPISODE,
            f"{attributes!r} -> {item.kind}, expected episode",
        )
        _check(
            item.title == show and item.season == season and item.episode == episode,
            f"{attributes!r} -> {item.title!r} S{item.season}E{item.episode}, "
            f"expected {show!r} S{season}E{episode}",
        )
        _check(
            item.episode_title == episode_title,
            f"{attributes!r} -> episode title {item.episode_title!r}, "
            f"expected {episode_title!r}",
        )


def test_companion_field_ignores_artists_and_channels() -> None:
    for artist in COMPANION_NON_EPISODES:
        item = parse_attributes(
            {"media_title": "Some Show", "media_artist": artist}
        ).item
        _check(
            item.kind != KIND_EPISODE,
            f"artist {artist!r} was mistaken for an episode reference",
        )


def test_title_regex_beats_the_companion_field() -> None:
    """A complete reference in the title wins over a companion field."""
    item = parse_attributes(
        {
            "media_title": "Matlock - S2 ∙ E2 - Un'altra Matlock",
            "media_artist": "Stagione 9, Ep. 9 Wrong",
        }
    ).item
    _check(
        item.season == 2 and item.episode == 2,
        f"expected S02E02 from the title, got S{item.season}E{item.episode}",
    )


def test_structured_attributes_beat_the_companion_field() -> None:
    item = parse_attributes(
        {
            "media_series_title": "Severance",
            "media_season": 2,
            "media_episode": 7,
            "media_title": "Chikhai Bardo",
            "media_artist": "S9 E9 Wrong",
        }
    ).item
    _check(
        item.season == 2 and item.episode == 7,
        f"expected S02E07 from attributes, got S{item.season}E{item.episode}",
    )


def test_season_and_episode_without_a_series_title() -> None:
    """Numbers with no series title: the title is the show's, not the episode's."""
    item = parse_attributes(
        {"media_title": "Silo", "media_season": 2, "media_episode": 3}
    ).item
    _check(item.kind == KIND_EPISODE, f"got {item.kind}, expected episode")
    _check(
        item.title == "Silo" and item.season == 2 and item.episode == 3,
        f"got {item.title!r} S{item.season}E{item.episode}, expected Silo S2E3",
    )


def test_episode_number_without_a_season() -> None:
    """The season is left open for the resolver, never assumed to be 1."""
    for raw, show, episode in EPISODE_ONLY_CASES:
        item = parse_attributes({"media_title": raw}).item
        _check(item is not None, f"{raw!r} did not parse")
        _check(item.kind == KIND_EPISODE, f"{raw!r} -> {item.kind}, expected episode")
        _check(item.title == show, f"{raw!r} -> {item.title!r}, expected {show!r}")
        _check(item.episode == episode, f"{raw!r} -> E{item.episode}, expected E{episode}")
        _check(item.season is None, f"{raw!r} assumed season {item.season}")
        _check(item.slug == f"{show} E{episode:02d}", f"{raw!r} -> slug {item.slug!r}")


def test_episode_number_without_a_season_ignores_lookalikes() -> None:
    for raw in EPISODE_ONLY_NON_MATCHES:
        item = parse_attributes({"media_title": raw}).item
        _check(
            item is None or item.kind != KIND_EPISODE,
            f"{raw!r} was mistaken for an episode",
        )


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as err:
            failures += 1
            print(f"FAIL  {test.__name__}: {err}")
        else:
            print(f"ok    {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
