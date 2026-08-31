"""Resolver tests, built from real Trakt search responses.

The fixtures below are the verbatim shape of `/search/movie?query=Dark Matter`
and `/search/show?query=Dark Matter` with `extended=full`, including Trakt's
actual relevance scores -- which matter, because their sheer magnitude is what
broke the runtime tie-breaker in the first place.

Runs standalone (``python tests/test_resolver.py``) as well as under pytest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.join(_HERE, "..", "custom_components", "trakt_scrobbler")

# resolver.py uses relative imports, so stand up a synthetic package for it.
# `api` and `image_match` are stubbed: the first drags in aiohttp, the second
# Pillow, and neither is needed to test how candidates are ranked.
_pkg = types.ModuleType("trakt_pkg")
_pkg.__path__ = [_COMPONENT]
sys.modules["trakt_pkg"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"trakt_pkg.{name}", os.path.join(_COMPONENT, f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"trakt_pkg.{name}"] = module
    spec.loader.exec_module(module)
    setattr(_pkg, name, module)
    return module


_api = types.ModuleType("trakt_pkg.api")


class TraktError(Exception):
    pass


class TraktNotFoundError(TraktError):
    pass


_api.TraktClient = object
_api.TraktError = TraktError
_api.TraktNotFoundError = TraktNotFoundError
sys.modules["trakt_pkg.api"] = _api
_pkg.api = _api

_image_match = types.ModuleType("trakt_pkg.image_match")
_image_match.available = lambda: False
sys.modules["trakt_pkg.image_match"] = _image_match
_pkg.image_match = _image_match

parser = _load("parser")
resolver = _load("resolver")

MediaItem = parser.MediaItem

# Trakt hands back the same relevance score for every hit in a response, and
# that score is ~1.16e18 -- a magnitude where one float64 step is 256.0.
SCORE = 1157451471441102848.0
LOWER_SCORE = 1157451437081364480.0


def _hit(key, score, **body):
    body.setdefault("ids", {"trakt": body.get("trakt_id", 0), "slug": body.get("slug", "x")})
    body.pop("trakt_id", None)
    return {"type": key, "score": score, key: body}


# `/search/movie?query=Dark Matter&extended=full`
MOVIES = [
    _hit("movie", SCORE, title="Dark Matter", year=2008, runtime=90,
         ids={"trakt": 11020, "slug": "dark-matter-2008"}),
    _hit("movie", SCORE, title="The Dark Matter of Love", year=2012, runtime=93,
         ids={"trakt": 124384, "slug": "the-dark-matter-of-love-2012"}),
    _hit("movie", SCORE, title="Dark Matter", year=2019, runtime=27,
         ids={"trakt": 492406, "slug": "dark-matter-2019"}),
    _hit("movie", SCORE, title="Pearl Jam: Dark Matter", year=2024, runtime=97,
         ids={"trakt": 1032196, "slug": "pearl-jam-dark-matter-2024"}),
    _hit("movie", SCORE, title="Dark Matter", year=None, runtime=None,
         ids={"trakt": 650490, "slug": "dark-matter"}),
]

# `/search/show?query=Dark Matter&extended=full`
SHOWS = [
    _hit("show", SCORE, title="Dark Matter", year=2024, runtime=55,
         ids={"trakt": 170597, "slug": "dark-matter-2024"}),
    _hit("show", SCORE, title="Dark Matter", year=2015, runtime=43,
         ids={"trakt": 96220, "slug": "dark-matter-2015"}),
    _hit("show", LOWER_SCORE, title="Dark Matters: Twisted But True", year=2011,
         runtime=43, ids={"trakt": 43590, "slug": "dark-matters"}),
    _hit("show", LOWER_SCORE, title="Heroes Reborn: Dark Matters", year=2015,
         runtime=5, ids={"trakt": 196017, "slug": "heroes-reborn-dark-matters"}),
]

#: what media_player.tv reported while S2E2 was playing
EPISODE_DURATION = 3547.0
#: the 2008 film's own runtime
MOVIE_DURATION = 5400.0


class StubClient:
    """Enough of TraktClient to drive the resolver."""

    def __init__(self):
        self.searches = []

    async def async_search(self, kind, query, year=None):
        self.searches.append((kind, query, year))
        return MOVIES if kind == "movie" else SHOWS

    async def async_get_watched_progress(self, show_id):
        return {"next_episode": {"season": 2, "number": 2}}

    async def async_get_episode(self, show_id, season, episode):
        return {
            "season": season,
            "number": episode,
            "title": "Trip of a Lifetime",
            "runtime": 59,
            "ids": {"trakt": 999001},
        }


def _resolve(duration, **kwargs):
    item = MediaItem(
        kind="ambiguous",
        title="Dark Matter",
        year=None,
        raw_title="Dark Matter",
        method="bare_title",
    )
    res = resolver.TraktResolver(StubClient(), next_episode_fallback=True)
    return asyncio.run(res.async_resolve(item, duration, **kwargs))


# ---------------------------------------------------------------------------
# The bug this file exists for
# ---------------------------------------------------------------------------


def test_trakt_score_cannot_absorb_a_runtime_bonus():
    """Why runtime is ranked separately instead of added to the score."""
    assert SCORE + 25.0 == SCORE
    assert SCORE - 20.0 == SCORE
    # ...so the two candidates were indistinguishable, and movie won the tie.
    assert (SCORE - 20.0) == (SCORE + 25.0)


def test_bare_title_picks_the_show_when_the_episode_length_fits():
    """A 59-minute "Dark Matter" is the 2024 series, not the 90-minute film."""
    resolved = _resolve(EPISODE_DURATION)
    assert resolved.kind == "episode", f"resolved as {resolved.kind}: {resolved.display}"
    assert resolved.show_id == 170597
    assert resolved.season == 2
    assert resolved.episode == 2


def test_bare_title_still_picks_the_movie_when_the_film_length_fits():
    """The same catalogue must still resolve the actual 90-minute film."""
    resolved = _resolve(MOVIE_DURATION)
    assert resolved.kind == "movie", f"resolved as {resolved.kind}: {resolved.display}"
    assert resolved.trakt_id == 11020
    assert resolved.method == "bare_title->movie"


def test_no_duration_keeps_the_historical_movie_preference():
    """With nothing to measure, behaviour is unchanged: the film wins the tie."""
    resolved = _resolve(None)
    assert resolved.kind == "movie"


# ---------------------------------------------------------------------------
# Ranking within a single search response
# ---------------------------------------------------------------------------


def test_rank_breaks_score_ties_on_runtime():
    """Trakt ties constantly, so runtime is what actually separates hits."""
    results = [
        _hit("show", SCORE, title="Wrong Length", year=2001, runtime=20,
             ids={"trakt": 1, "slug": "a"}),
        _hit("show", SCORE, title="Right Length", year=2002, runtime=55,
             ids={"trakt": 2, "slug": "b"}),
    ]
    assert resolver._rank(results, "show", EPISODE_DURATION).body["ids"]["trakt"] == 2


def test_rank_keeps_trakt_relevance_ahead_of_runtime():
    """A show's Trakt runtime is an average, so it must not override relevance.

    Ted Lasso is listed at 45 minutes though season one runs nearer 31; if
    runtime outranked relevance inside a single search, an irrelevant show of
    exactly the right length would win.
    """
    results = [
        _hit("show", SCORE, title="Ted Lasso", year=2020, runtime=45,
             ids={"trakt": 1, "slug": "ted-lasso"}),
        _hit("show", LOWER_SCORE, title="Ted Something Else", year=2011, runtime=31,
             ids={"trakt": 2, "slug": "other"}),
    ]
    assert resolver._rank(results, "show", 1860.0).body["ids"]["trakt"] == 1


def test_rank_prefers_unknown_runtime_over_a_measured_mismatch():
    """Missing data beats data that positively rules a candidate out."""
    results = [
        _hit("movie", SCORE, title="Way Too Long", year=2001, runtime=200,
             ids={"trakt": 1, "slug": "a"}),
        _hit("movie", SCORE, title="No Runtime Listed", year=2002, runtime=None,
             ids={"trakt": 2, "slug": "b"}),
    ]
    assert resolver._rank(results, "movie", EPISODE_DURATION).body["ids"]["trakt"] == 2


def test_rank_breaks_score_ties_on_trakt_ordering():
    results = [
        _hit("movie", SCORE, title="First", year=2001, runtime=59,
             ids={"trakt": 1, "slug": "a"}),
        _hit("movie", SCORE, title="Second", year=2002, runtime=59,
             ids={"trakt": 2, "slug": "b"}),
    ]
    assert resolver._rank(results, "movie", EPISODE_DURATION).body["ids"]["trakt"] == 1


def test_kind_rank_breaks_a_runtime_tie_on_an_exact_title():
    """Both fit the runtime, so only the title can separate them.

    Trakt's top movie hit for "Shogun" is a 65-minute Kamen Rider film whose
    name merely contains the word; the series is 62 minutes. Without the title
    check the tie fell to the film.
    """
    movie = resolver._rank(
        [_hit("movie", SCORE, title="Kamen Rider OOO Wonderful: The Shogun and the "
              "21 Core Medals", year=2012, runtime=65, ids={"trakt": 1, "slug": "a"})],
        "movie", 3540.0, "Shogun",
    )
    show = resolver._rank(
        [_hit("show", SCORE, title="Shōgun", year=2024, runtime=62,
              ids={"trakt": 2, "slug": "shogun-2024"})],
        "show", 3540.0, "Shogun",
    )
    assert movie.fit == show.fit == resolver._FIT_MATCH  # runtime cannot decide
    assert show.kind_rank > movie.kind_rank


def test_fold_ignores_case_accents_and_punctuation():
    assert resolver._fold("Shōgun") == resolver._fold("Shogun")
    assert resolver._fold("The Terminal List") == resolver._fold("the terminal list")
    assert resolver._fold("FARGO: SEASON 5") != resolver._fold("Fargo")
    assert resolver._fold(None) == ""


def test_rank_returns_none_for_an_empty_response():
    assert resolver._rank([], "movie", EPISODE_DURATION) is None


def test_runtime_fit_classification():
    assert resolver._runtime_fit(55, EPISODE_DURATION) == resolver._FIT_MATCH
    assert resolver._runtime_fit(90, EPISODE_DURATION) == resolver._FIT_MISMATCH
    assert resolver._runtime_fit(None, EPISODE_DURATION) == resolver._FIT_UNKNOWN
    assert resolver._runtime_fit(55, None) == resolver._FIT_UNKNOWN
    assert resolver._runtime_fit("nonsense", EPISODE_DURATION) == resolver._FIT_UNKNOWN
    assert resolver._runtime_fit(0, EPISODE_DURATION) == resolver._FIT_UNKNOWN


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
