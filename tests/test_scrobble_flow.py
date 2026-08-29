"""End-to-end checks of the scrobble state machine against a stubbed Trakt.

Needs the `homeassistant` package importable, so run it inside a Home Assistant
container:

    docker exec homeassistant python /config/test_scrobble_flow.py
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, "/config")

from custom_components.trakt_scrobbler.const import (  # noqa: E402
    CONF_EXCLUDED_APPS,
    CONF_HEARTBEAT,
    CONF_MIN_DURATION,
    CONF_NEXT_EPISODE_FALLBACK,
    CONF_PLAYERS,
    DEFAULT_EXCLUDED_APPS,
)
from custom_components.trakt_scrobbler.coordinator import ScrobbleManager  # noqa: E402

PLAYER = "media_player.tv"

# What the Apple TV actually reports while Infuse is playing.
INFUSE = {
    "media_content_type": "video",
    "media_duration": 2815,
    "media_title": "Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio",
    "app_id": "com.firecore.infuse",
    "app_name": "Infuse",
}

# ...and while the built-in TV app is playing.
APPLE_TV_APP = {
    "media_content_id": "A0006504004",
    "media_content_type": "video",
    "media_duration": 2819,
    "media_title": "Ted Lasso",
    "app_id": "com.apple.TVWatchList",
    "app_name": "TV",
}


class FakeTrakt:
    """Records scrobbles and answers lookups from a tiny fixed catalogue."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_episode: dict | None = None

    async def async_get_settings(self) -> dict:
        return {"user": {"username": "tester"}}

    async def async_search(self, kind: str, query: str, year=None) -> list[dict]:
        catalogue = {
            ("show", "spider-noir"): {
                "title": "Spider-Noir",
                "year": 2026,
                "runtime": 47,
                "ids": {"trakt": 111, "slug": "spider-noir"},
            },
            ("show", "ted lasso"): {
                "title": "Ted Lasso",
                "year": 2020,
                "runtime": 47,
                "ids": {"trakt": 222, "slug": "ted-lasso"},
            },
        }
        hit = catalogue.get((kind, query.lower()))
        return [{"type": kind, "score": 1000.0, kind: hit}] if hit else []

    async def async_get_episode(self, show_id: int, season: int, number: int) -> dict:
        return {
            "season": season,
            "number": number,
            "title": f"Episode {number}",
            "runtime": 47,
            "ids": {"trakt": show_id * 1000 + season * 100 + number},
        }

    async def async_get_watched_progress(self, show_id: int) -> dict:
        return {"next_episode": self.next_episode}

    async def async_scrobble(self, action: str, payload: dict) -> dict:
        self.calls.append((action, payload))
        return {"action": action}


def build(**options) -> tuple[ScrobbleManager, FakeTrakt]:
    # These tests exercise the state machine, not the app filter, so they run
    # with no exclusions unless a test opts in. (Infuse is in the shipped
    # default list, which would otherwise skip the INFUSE fixture below.)
    settings = {
        CONF_PLAYERS: [PLAYER],
        CONF_EXCLUDED_APPS: [],
        CONF_MIN_DURATION: 300,
        CONF_HEARTBEAT: 300,
        CONF_NEXT_EPISODE_FALLBACK: True,
    }
    settings.update(options)
    entry = SimpleNamespace(options=settings, data={}, entry_id="test")
    client = FakeTrakt()
    return ScrobbleManager(hass=None, entry=entry, client=client), client


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _at(attributes: dict, position: float) -> dict:
    """Attributes with a reported playback position."""
    return {**attributes, "media_position": position}


def test_full_watch_cycle() -> None:
    """play -> pause -> resume -> off produces start/pause/start/stop."""
    manager, trakt = build()

    async def run() -> None:
        await manager._evaluate(PLAYER, "playing", _at(INFUSE, 0))
        await manager._evaluate(PLAYER, "paused", _at(INFUSE, 300))
        await manager._evaluate(PLAYER, "playing", _at(INFUSE, 300))
        await manager._evaluate(PLAYER, "off", {})

    asyncio.run(run())

    actions = [action for action, _ in trakt.calls]
    _check(actions == ["start", "pause", "start", "stop"], f"got {actions}")

    _, first = trakt.calls[0]
    _check(first["show"]["ids"]["trakt"] == 111, f"wrong show: {first}")
    _check(
        first["episode"] == {"season": 1, "number": 1},
        f"wrong episode: {first['episode']}",
    )
    _check(first["progress"] < 1, f"start progress should be ~0, got {first['progress']}")

    _, pause = trakt.calls[1]
    _check(10 < pause["progress"] < 12, f"pause progress {pause['progress']}")

    _, stop = trakt.calls[3]
    _check(10 < stop["progress"] < 12, f"stop progress {stop['progress']}")


def test_watched_threshold_progress() -> None:
    """Finishing an episode reports a progress Trakt will mark as watched."""
    manager, trakt = build()

    async def run() -> None:
        await manager._evaluate(PLAYER, "playing", _at(INFUSE, 0))
        await manager._evaluate(PLAYER, "playing", _at(INFUSE, 2800))
        await manager._evaluate(PLAYER, "idle", {})

    asyncio.run(run())
    action, payload = trakt.calls[-1]
    _check(action == "stop", f"expected stop, got {action}")
    _check(payload["progress"] >= 80, f"progress {payload['progress']} < 80")


def test_switching_item_stops_the_previous_one() -> None:
    manager, trakt = build()
    other = {**INFUSE, "media_title": "Spider-Noir - S1 ∙ E2 - Il secondo"}

    async def run() -> None:
        await manager._evaluate(PLAYER, "playing", _at(INFUSE, 100))
        await manager._evaluate(PLAYER, "playing", _at(other, 0))

    asyncio.run(run())
    actions = [a for a, _ in trakt.calls]
    _check(actions == ["start", "stop", "start"], f"got {actions}")
    _check(trakt.calls[0][1]["episode"]["number"] == 1, "first should be E1")
    _check(trakt.calls[2][1]["episode"]["number"] == 2, "third should be E2")


def test_heartbeat_does_not_spam_trakt() -> None:
    """Repeated 'still playing' updates must not re-POST every time."""
    manager, trakt = build()

    async def run() -> None:
        for position in range(0, 600, 30):
            await manager._evaluate(PLAYER, "playing", _at(INFUSE, position))

    asyncio.run(run())
    _check(len(trakt.calls) == 1, f"expected 1 scrobble, got {len(trakt.calls)}")


def test_excluded_app_is_skipped() -> None:
    manager, trakt = build(**{CONF_EXCLUDED_APPS: ["youtube"]})
    youtube = {
        "media_content_type": "video",
        "media_duration": 900,
        "media_title": "Some Video - S1 ∙ E1 - Clip",
        "app_id": "com.google.ios.youtube",
        "app_name": "YouTube",
    }

    asyncio.run(manager._evaluate(PLAYER, "playing", youtube))
    _check(not trakt.calls, f"YouTube should be skipped, got {trakt.calls}")
    _check(
        manager.ignored_reason(PLAYER) == "excluded_app:youtube",
        f"got {manager.ignored_reason(PLAYER)}",
    )


def test_infuse_excluded_by_default() -> None:
    """Infuse scrobbles to Trakt itself, so the shipped default skips it."""
    assert "infuse" in DEFAULT_EXCLUDED_APPS
    manager, trakt = build(**{CONF_EXCLUDED_APPS: DEFAULT_EXCLUDED_APPS})
    asyncio.run(manager._evaluate(PLAYER, "playing", _at(INFUSE, 100)))
    _check(not trakt.calls, f"Infuse should be skipped, got {trakt.calls}")
    _check(
        manager.ignored_reason(PLAYER) == "excluded_app:infuse",
        f"got {manager.ignored_reason(PLAYER)}",
    )


def test_short_content_is_skipped() -> None:
    manager, trakt = build()
    trailer = {**INFUSE, "media_duration": 90}
    asyncio.run(manager._evaluate(PLAYER, "playing", trailer))
    _check(not trakt.calls, f"trailer should be skipped, got {trakt.calls}")
    _check(manager.ignored_reason(PLAYER) == "too_short", "expected too_short")


def test_next_episode_guess_for_apple_tv_app() -> None:
    """A bare series name resolves via Trakt watched progress."""
    manager, trakt = build()
    trakt.next_episode = {"season": 3, "number": 4}

    asyncio.run(manager._evaluate(PLAYER, "playing", _at(APPLE_TV_APP, 10)))

    _check(len(trakt.calls) == 1, f"expected one start, got {trakt.calls}")
    action, payload = trakt.calls[0]
    _check(action == "start", f"got {action}")
    _check(payload["show"]["ids"]["trakt"] == 222, f"wrong show: {payload}")
    _check(
        payload["episode"] == {"season": 3, "number": 4},
        f"expected S03E04, got {payload['episode']}",
    )

    session = manager.sessions[PLAYER]
    _check(session.resolved.guessed is True, "the guess must be flagged")
    _check(
        session.resolved.method == "next_episode_guess",
        f"got {session.resolved.method}",
    )


def test_next_episode_guess_can_be_disabled() -> None:
    manager, trakt = build(**{CONF_NEXT_EPISODE_FALLBACK: False})
    asyncio.run(manager._evaluate(PLAYER, "playing", _at(APPLE_TV_APP, 10)))
    _check(not trakt.calls, f"nothing should be scrobbled, got {trakt.calls}")
    _check(
        manager.sessions[PLAYER].reason == "no_episode_number",
        f"got {manager.sessions[PLAYER].reason}",
    )


def test_first_episode_when_nothing_watched_yet() -> None:
    manager, trakt = build()
    trakt.next_episode = None  # never watched this show
    asyncio.run(manager._evaluate(PLAYER, "playing", _at(APPLE_TV_APP, 10)))
    _check(
        trakt.calls[0][1]["episode"] == {"season": 1, "number": 1},
        f"expected S01E01, got {trakt.calls[0][1]['episode']}",
    )



def test_wall_clock_progress_without_position() -> None:
    """The Apple TV often omits media_position; progress must still advance."""
    manager, _ = build()
    asyncio.run(manager._evaluate(PLAYER, "playing", INFUSE))
    session = manager.sessions[PLAYER]
    _check(session.position is None, "no position should have been recorded")
    _check(session.playing_since is not None, "wall-clock tracking should be running")
    session.accumulated = 1500
    session.playing_since = None
    session.player_state = "paused"
    _check(52 < session.progress < 54, f"progress {session.progress}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as err:
            failures += 1
            print(f"FAIL  {test.__name__}: {err}")
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok    {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
