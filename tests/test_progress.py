"""Progress tracking tests, built from a session this integration really saw.

The Apple TV refreshes ``media_position`` only every few minutes, so the session
has to extrapolate between reports. The interesting case is what happens when
playback stops: the position that arrives with the pause can be a quarter of an
hour behind, and believing it rewinds the session far enough that the ``stop``
scrobble lands below Trakt's 80% watched threshold.

Runs standalone (``python tests/test_progress.py``) as well as under pytest.
`homeassistant` is stubbed rather than installed, so this needs no extra
dependencies.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_COMPONENT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "custom_components",
    "trakt_scrobbler",
)

# --------------------------------------------------------------------------
# Stubs. coordinator.py needs a handful of Home Assistant names and aiohttp;
# none of their behaviour matters to progress tracking except the clock.
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 2, 18, 55, 14, tzinfo=timezone.utc)


def _module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_module(
    "aiohttp",
    ClientError=type("ClientError", (Exception,), {}),
    ClientResponse=type("ClientResponse", (), {}),
    ClientSession=type("ClientSession", (), {}),
)
_module("homeassistant")
_module(
    "homeassistant.const",
    STATE_IDLE="idle",
    STATE_OFF="off",
    STATE_PAUSED="paused",
    STATE_PLAYING="playing",
    STATE_STANDBY="standby",
    STATE_UNAVAILABLE="unavailable",
    STATE_UNKNOWN="unknown",
)
_module(
    "homeassistant.core",
    CALLBACK_TYPE=object,
    Event=dict,
    EventStateChangedData=dict,
    HomeAssistant=type("HomeAssistant", (), {}),
)
_module("homeassistant.helpers")
_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda *a: None)
_module(
    "homeassistant.helpers.event",
    async_track_state_change_event=lambda *a: None,
    async_track_time_interval=lambda *a: None,
)
_module("homeassistant.helpers.network", get_url=lambda *a, **k: "")
_module("homeassistant.util")
# The clock the session extrapolates against; tests move it explicitly.
_module("homeassistant.util.dt", utcnow=lambda: NOW)

# Load coordinator.py under a throwaway package so its relative imports
# (`.api`, `.parser`, ...) resolve to the real modules beside it.
_PKG = "trakt_scrobbler_under_test"
_package = types.ModuleType(_PKG)
_package.__path__ = [_COMPONENT]
sys.modules[_PKG] = _package
_spec = importlib.util.spec_from_file_location(
    f"{_PKG}.coordinator", os.path.join(_COMPONENT, "coordinator.py")
)
coordinator = importlib.util.module_from_spec(_spec)
sys.modules[f"{_PKG}.coordinator"] = coordinator
_spec.loader.exec_module(coordinator)

ScrobbleSession = coordinator.ScrobbleSession

# --------------------------------------------------------------------------
# The session, as recorded on 2026-09-02.
# --------------------------------------------------------------------------

START = datetime(2026, 9, 2, 18, 55, 14, tzinfo=timezone.utc)
DURATION = 2434  # 40:34

# (seconds after the start, player state, the media_position reported with it).
# pyatv only refreshes its position on a transition, so the value arriving with
# the final pause is ~16 minutes behind where playback had actually got to.
TRACE = [
    (0, "playing", 0),
    (13, "paused", 13),
    (287, "playing", 13),
    (1458, "paused", 1184),
    (1642, "playing", 1184),
    (2669, "paused", 1239),  # stale: playback is really at ~2211
]
#: seconds the player actually spent playing across that trace
WATCHED = 13 + (1458 - 287) + (2669 - 1642)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _at(offset: float) -> datetime:
    return START + timedelta(seconds=offset)


def _session() -> "ScrobbleSession":
    session = ScrobbleSession(entity_id="media_player.tv", fingerprint=("x",))
    session.duration = float(DURATION)
    return session


def _report(session, offset, state, position, stamp_offset=None):
    """Feed one player update, with the clock pinned to ``offset``."""
    global NOW
    NOW = _at(offset)
    attributes = {"media_duration": DURATION}
    if position is not None:
        attributes["media_position"] = position
        attributes["media_position_updated_at"] = _at(
            offset if stamp_offset is None else stamp_offset
        )
    session.update_playback(state, attributes)


def _replay():
    session = _session()
    for offset, state, position in TRACE:
        _report(session, offset, state, position)
    return session


def test_progress_extrapolates_between_position_reports() -> None:
    """While playing, the session runs the clock past a stale position."""
    global NOW
    session = _session()
    _report(session, 0, "playing", 0)
    NOW = _at(600)
    _check(
        abs(session.elapsed - 600) < 1,
        f"expected ~600s elapsed after 10 minutes, got {session.elapsed:.0f}s",
    )


def test_a_stale_position_on_pause_does_not_rewind() -> None:
    """The regression: believing the pause report drops 92.6% to 50.9%."""
    session = _replay()
    global NOW
    NOW = _at(2669)
    expected = WATCHED / DURATION * 100
    _check(
        abs(session.progress - expected) < 1.5,
        f"expected ~{expected:.1f}% at the stop, got {session.progress:.1f}%",
    )
    _check(
        session.progress >= 80,
        f"{session.progress:.1f}% would not be marked watched by Trakt",
    )


def test_a_seek_within_a_state_is_still_believed() -> None:
    """Only reports that arrive on a state change are distrusted."""
    session = _replay()
    _report(session, 2700, "paused", 600)
    global NOW
    NOW = _at(2700)
    expected = 600 / DURATION * 100
    _check(
        abs(session.progress - expected) < 0.5,
        f"a real seek to 10:00 should read {expected:.1f}%, got {session.progress:.1f}%",
    )


def test_a_forward_jump_on_a_state_change_is_believed() -> None:
    """Skipping ahead then pausing must not be clamped backwards."""
    global NOW
    session = _session()
    _report(session, 0, "playing", 0)
    _report(session, 60, "paused", 2000)
    NOW = _at(60)
    expected = 2000 / DURATION * 100
    _check(
        abs(session.progress - expected) < 0.5,
        f"expected {expected:.1f}% after skipping ahead, got {session.progress:.1f}%",
    )


def test_players_without_a_position_use_the_wall_clock() -> None:
    """Some players report no position at all; the accumulator covers them."""
    global NOW
    session = _session()
    _report(session, 0, "playing", None)
    _report(session, 1200, "paused", None)
    NOW = _at(1500)  # paused for five minutes; the clock must not keep running
    _check(
        abs(session.elapsed - 1200) < 1,
        f"expected 1200s accumulated while paused, got {session.elapsed:.0f}s",
    )


def test_progress_is_clamped_to_the_range_trakt_accepts() -> None:
    global NOW
    session = _session()
    _report(session, 0, "playing", 0)
    NOW = _at(DURATION * 2)
    _check(session.progress <= 100, f"progress ran past 100: {session.progress}")


def test_trakt_rejects_scrobbles_below_one_percent() -> None:
    """The floor exists because Trakt 422s a pause or stop under 1%."""
    _check(
        coordinator._MIN_SCROBBLE_PROGRESS >= 1.0,
        f"floor is {coordinator._MIN_SCROBBLE_PROGRESS}, Trakt requires 1.0",
    )


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
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
