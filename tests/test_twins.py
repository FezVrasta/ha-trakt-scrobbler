"""Two shows, one title.

Trakt lists "The Grand Tour" twice -- the 2016 Amazon series and the 2026
revival -- with the *same* relevance score, so the ranking has nothing to
separate them and the older one wins simply by being listed first. These are
the signals that settle it instead: the episode still, the episode's own
runtime, and whether the episode exists in that show at all.

Fixture numbers are the real ones from `/search/show?query=The Grand Tour` and
the two shows' season listings.

Runs standalone (``python tests/test_twins.py``) as well as under pytest.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import types
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.join(_HERE, "..", "custom_components", "trakt_scrobbler")

_pkg = types.ModuleType("twins_pkg")
_pkg.__path__ = [_COMPONENT]
sys.modules["twins_pkg"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"twins_pkg.{name}", os.path.join(_COMPONENT, f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"twins_pkg.{name}"] = module
    spec.loader.exec_module(module)
    setattr(_pkg, name, module)
    return module


_api = types.ModuleType("twins_pkg.api")


class TraktError(Exception):
    pass


class TraktNotFoundError(TraktError):
    pass


_api.TraktClient = object
_api.TraktError = TraktError
_api.TraktNotFoundError = TraktNotFoundError
sys.modules["twins_pkg.api"] = _api
_pkg.api = _api


# ---------------------------------------------------------------------------
# A working stand-in for image_match: Pillow and the network are not available
# here, but the matching *rules* are what the resolver depends on.
# ---------------------------------------------------------------------------

#: url -> dhash, standing in for "fetch the still and hash it"
STILLS: dict[str, int] = {}


def _still(url: str) -> int:
    """A deterministic 256-bit hash for a still, as dissimilar as two frames.

    Two unrelated 256-bit hashes sit ~128 bits apart, which is exactly the
    separation real stills show: the right episode scored 3, every other one
    above 100.
    """
    value = int.from_bytes(hashlib.sha256(url.encode()).digest(), "big")
    STILLS[url] = value
    return value


@dataclass
class ThumbnailMatch:
    season: int
    episode: int
    distance: int
    runner_up: int | None
    title: str | None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


async def _async_fetch_and_hash(session, url, headers=None):
    return STILLS.get(url)


async def _async_best_match(session, target, candidates):
    scored = sorted(
        (_hamming(target, STILLS[c["image"]]), c)
        for c in candidates
        if c["image"] in STILLS
    )
    if not scored:
        return None
    distance, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    if distance > _image_match.MAX_DISTANCE:
        return None
    if runner_up is not None and runner_up - distance < 40:
        return None
    return ThumbnailMatch(
        season=best["season"],
        episode=best["episode"],
        distance=distance,
        runner_up=runner_up,
        title=best.get("title"),
    )


_image_match = types.ModuleType("twins_pkg.image_match")
_image_match.available = lambda: True
_image_match.MAX_DISTANCE = 24
_image_match.hamming = _hamming
_image_match.https = lambda url: url
_image_match.async_fetch_and_hash = _async_fetch_and_hash
_image_match.async_best_match = _async_best_match
sys.modules["twins_pkg.image_match"] = _image_match
_pkg.image_match = _image_match

parser = _load("parser")
resolver = _load("resolver")

MediaItem = parser.MediaItem

# Trakt gives every hit in a response the same relevance score.
SCORE = 1157451471441102848.0

OLD = {"title": "The Grand Tour", "year": 2016, "runtime": 60,
       "ids": {"trakt": 1, "slug": "the-grand-tour"}}
NEW = {"title": "The Grand Tour", "year": 2026, "runtime": 40,
       "ids": {"trakt": 2, "slug": "the-grand-tour-2026"}}

#: Trakt lists the 2016 series first.
SHOW_RESULTS = [
    {"type": "show", "score": SCORE, "show": OLD},
    {"type": "show", "score": SCORE, "show": NEW},
]

# Real runtimes: the 2016 launch special runs 130 minutes, the 2026 one 58.
EPISODES = {
    1: {
        (1, 1): ("The Holy Trinity", 130),
        (1, 2): ("Operation Desert Stumble", 58),
        (1, 13): ("Past v Future", 60),
    },
    2: {
        (1, 1): ("The Next Generation", 58),
        (1, 2): ("Sweat, Sports Cars and Singapore", 52),
        (1, 3): ("It's OK to Be Kei", 47),
    },
}


def _episode(show_id: int, season: int, number: int) -> dict:
    title, runtime = EPISODES[show_id][(season, number)]
    url = f"still/{show_id}/{season}/{number}"
    _still(url)
    return {
        "season": season,
        "number": number,
        "title": title,
        "runtime": runtime,
        "ids": {"trakt": show_id * 1000 + number},
        "images": {"screenshot": [url]},
    }


#: the frame on screen: the 2026 series, season 1 episode 3
ON_SCREEN = _still("still/2/1/3")


class StubClient:
    """Enough of TraktClient to drive the resolver, with call counting."""

    session = None

    def __init__(self, shows=None):
        self.shows = SHOW_RESULTS if shows is None else shows
        self.searches: list[tuple] = []
        self.season_lookups: list[int] = []

    async def async_search(self, kind, query, year=None):
        self.searches.append((kind, query, year))
        return [] if kind == "movie" else self.shows

    async def async_get_seasons(self, show_id):
        self.season_lookups.append(show_id)
        return [{"number": 1}]

    async def async_get_season_episodes(self, show_id, season):
        return [
            _episode(show_id, s, n) for (s, n) in EPISODES[show_id] if s == season
        ]

    async def async_get_episode(self, show_id, season, episode):
        if (season, episode) not in EPISODES[show_id]:
            raise TraktNotFoundError("no such episode")
        return _episode(show_id, season, episode)

    async def async_get_watched_progress(self, show_id):
        return {"next_episode": {"season": 1, "number": 1}}


def _show_item(title="The Grand Tour"):
    return MediaItem(
        kind=parser.KIND_SHOW, title=title, raw_title=title, method="bare_title"
    )


def _episode_item(season, number, title="The Grand Tour"):
    return MediaItem(
        kind=parser.KIND_EPISODE,
        title=title,
        season=season,
        episode=number,
        raw_title=title,
        method="companion_media_artist",
    )


def _resolve(client, item, duration=None, thumbnail=None):
    res = resolver.TraktResolver(client, next_episode_fallback=True)
    return asyncio.run(res.async_resolve(item, duration, thumbnail))


# ---------------------------------------------------------------------------
# The bug this file exists for
# ---------------------------------------------------------------------------


def test_trakt_cannot_separate_two_shows_of_the_same_name():
    candidates = resolver._rank_all(SHOW_RESULTS, "show", None, "The Grand Tour")
    assert [c.body["year"] for c in candidates] == [2016, 2026]
    assert candidates[0].rank > candidates[1].rank  # only because it is first
    assert len(resolver._twins(candidates)) == 2


def test_a_bare_title_without_artwork_still_falls_to_the_first_hit():
    """Unchanged behaviour when there is nothing to decide with."""
    resolved = _resolve(StubClient(), _show_item())
    assert resolved.show_id == 1
    assert resolved.guessed


def test_artwork_picks_the_revival_out_of_both_shows():
    """The frame on screen belongs to the 2026 series, so that is what airs."""
    resolved = _resolve(StubClient(), _show_item(), thumbnail=ON_SCREEN)
    assert resolved.show_id == 2, f"picked {resolved.display}"
    assert (resolved.season, resolved.episode) == (1, 3)
    assert resolved.method == "thumbnail_match"
    assert not resolved.guessed


def test_the_choice_is_remembered_for_the_next_lookup():
    """Having paid for the comparison once, don't walk both catalogues again."""
    client = StubClient()
    res = resolver.TraktResolver(client, next_episode_fallback=True)
    first = asyncio.run(res.async_resolve(_show_item(), None, ON_SCREEN))
    assert first.show_id == 2
    assert set(client.season_lookups) == {1, 2}

    client.season_lookups.clear()
    second = asyncio.run(res.async_resolve(_show_item(), None, ON_SCREEN))
    assert second.show_id == 2
    assert client.season_lookups == [2], "the wrong show was searched again"


# ---------------------------------------------------------------------------
# A player that reports S/E but not which show
# ---------------------------------------------------------------------------


def test_episode_runtime_separates_the_twins():
    """A 58-minute S01E01 is the revival; the 2016 special runs 130."""
    resolved = _resolve(StubClient(), _episode_item(1, 1), duration=58 * 60)
    assert resolved.show_id == 2, f"picked {resolved.display}"
    assert resolved.extra["show_title"] == "The Grand Tour"


def test_episode_runtime_still_finds_the_original():
    resolved = _resolve(StubClient(), _episode_item(1, 1), duration=130 * 60)
    assert resolved.show_id == 1, f"picked {resolved.display}"


def test_an_episode_only_one_show_has_rules_the_other_out():
    """Only the 2016 series ever reached a thirteenth episode."""
    resolved = _resolve(StubClient(), _episode_item(1, 13))
    assert resolved.show_id == 1


def test_artwork_separates_twins_whose_episode_runtimes_both_fit():
    """52 and 58 minutes are both plausible for a 52-minute episode."""
    both_fit = 52 * 60
    assert resolver._runtime_fit(52, both_fit) == resolver._FIT_MATCH
    assert resolver._runtime_fit(58, both_fit) == resolver._FIT_MATCH

    resolved = _resolve(
        StubClient(),
        _episode_item(1, 2),
        duration=both_fit,
        thumbnail=lambda: _ready(STILLS["still/2/1/2"]),
    )
    assert resolved.show_id == 2, f"picked {resolved.display}"


def test_the_artwork_is_not_fetched_when_the_title_is_unmistakable():
    """One show by that name: the image fetch must not happen at all."""
    calls = []

    def loader():
        calls.append(1)
        return _ready(ON_SCREEN)

    client = StubClient(shows=[{"type": "show", "score": SCORE, "show": NEW}])
    resolved = _resolve(client, _episode_item(1, 2), thumbnail=loader)
    assert resolved.show_id == 2
    assert calls == [], "artwork was fetched for an unambiguous title"


def test_a_coin_toss_is_not_remembered():
    """Nothing decided it, so the next lookup must not inherit the guess."""
    client = StubClient()
    res = resolver.TraktResolver(client, next_episode_fallback=True)
    # S01E02 exists in both shows, no duration, no artwork.
    asyncio.run(res.async_resolve(_episode_item(1, 2)))
    assert res._twin_choice == {}


def test_a_broken_artwork_loader_does_not_break_resolution():
    def loader():
        raise RuntimeError("player went away")

    resolved = _resolve(StubClient(), _show_item(), thumbnail=loader)
    assert resolved.show_id == 1
    assert resolved.guessed


async def _ready(value):
    return value


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
