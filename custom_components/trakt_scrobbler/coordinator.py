"""Watches media players and keeps Trakt in step with them."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.const import (
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.network import get_url
from homeassistant.util import dt as dt_util

from .api import (
    TraktAuthError,
    TraktClient,
    TraktError,
    TraktRateLimitError,
    Tokens,
)
from .const import (
    ACTION_PAUSE,
    ACTION_START,
    ACTION_STOP,
    CONF_EXCLUDED_APPS,
    CONF_HEARTBEAT,
    CONF_MIN_DURATION,
    CONF_NEXT_EPISODE_FALLBACK,
    CONF_PLAYERS,
    CONF_THUMBNAIL_MATCH,
    DEFAULT_EXCLUDED_APPS,
    DEFAULT_HEARTBEAT,
    DEFAULT_MIN_DURATION,
    DEFAULT_NEXT_EPISODE_FALLBACK,
    DEFAULT_THUMBNAIL_MATCH,
    IGNORED_CONTENT_TYPES,
    STATE_ERROR,
    STATE_UNMATCHED,
    STATE_WATCHING,
    TICK_INTERVAL,
)
from . import image_match
from .parser import KIND_SHOW, MediaItem, parse_attributes
from .resolver import Resolved, ResolutionError, TraktResolver

_LOGGER = logging.getLogger(__name__)

#: player states we treat as "content is on screen"
_ACTIVE_STATES = {STATE_PLAYING, STATE_PAUSED, "buffering"}
#: player states that end a session
_DEAD_STATES = {STATE_IDLE, STATE_OFF, STATE_STANDBY, STATE_UNAVAILABLE, STATE_UNKNOWN}

#: don't re-send `start` unless progress moved at least this much
_PROGRESS_EPSILON = 1.0


@dataclass
class ScrobbleSession:
    """One continuous viewing of one item on one player."""

    entity_id: str
    fingerprint: tuple
    item: MediaItem | None = None
    resolved: Resolved | None = None

    status: str = STATE_UNMATCHED
    reason: str | None = None
    error: str | None = None

    duration: float | None = None
    player_state: str = STATE_PLAYING

    # Position reported by the player, when it reports one at all.
    position: float | None = None
    position_updated_at: datetime | None = None

    # Wall-clock fallback for players that don't report a position.
    accumulated: float = 0.0
    playing_since: datetime | None = None

    last_action: str | None = None
    last_sent_progress: float | None = None
    last_sent_at: datetime | None = None
    started_at: datetime = field(default_factory=dt_util.utcnow)

    def update_playback(self, state: str, attributes: dict[str, Any]) -> None:
        """Fold a new player state into the session's progress tracking."""
        now = dt_util.utcnow()
        was_playing = self.player_state == STATE_PLAYING

        if (duration := attributes.get("media_duration")) is not None:
            try:
                self.duration = float(duration)
            except (TypeError, ValueError):
                pass

        position = attributes.get("media_position")
        if position is not None:
            try:
                self.position = float(position)
            except (TypeError, ValueError):
                self.position = None
            updated = attributes.get("media_position_updated_at")
            self.position_updated_at = updated if isinstance(updated, datetime) else now

        # Maintain the wall-clock accumulator across play/pause transitions.
        if was_playing and self.playing_since is not None:
            self.accumulated += (now - self.playing_since).total_seconds()
            self.playing_since = None

        self.player_state = state
        if state == STATE_PLAYING:
            self.playing_since = now

    @property
    def elapsed(self) -> float:
        """Seconds of content watched, best guess."""
        now = dt_util.utcnow()
        if self.position is not None:
            elapsed = self.position
            if self.player_state == STATE_PLAYING and self.position_updated_at:
                elapsed += (now - self.position_updated_at).total_seconds()
        else:
            elapsed = self.accumulated
            if self.player_state == STATE_PLAYING and self.playing_since:
                elapsed += (now - self.playing_since).total_seconds()
        return max(elapsed, 0.0)

    @property
    def progress(self) -> float:
        """Watched percentage, clamped to Trakt's accepted 0-100 range."""
        if not self.duration:
            return 0.0
        return min(max(self.elapsed / self.duration * 100, 0.0), 100.0)


class ScrobbleManager:
    """Owns the player subscriptions and the Trakt conversation."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        client: TraktClient,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.sessions: dict[str, ScrobbleSession] = {}
        self.last_error: str | None = None
        #: entity_id -> why we are deliberately not scrobbling it
        self._ignored: dict[str, str] = {}

        self._resolver = TraktResolver(
            client, next_episode_fallback=self.next_episode_fallback
        )
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def _option(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, default)

    @property
    def players(self) -> list[str]:
        return list(self._option(CONF_PLAYERS, []))

    @property
    def excluded_apps(self) -> list[str]:
        return [
            app.lower()
            for app in self._option(CONF_EXCLUDED_APPS, DEFAULT_EXCLUDED_APPS)
            if app
        ]

    @property
    def min_duration(self) -> float:
        return float(self._option(CONF_MIN_DURATION, DEFAULT_MIN_DURATION))

    @property
    def next_episode_fallback(self) -> bool:
        return bool(
            self._option(CONF_NEXT_EPISODE_FALLBACK, DEFAULT_NEXT_EPISODE_FALLBACK)
        )

    @property
    def thumbnail_match(self) -> bool:
        return bool(self._option(CONF_THUMBNAIL_MATCH, DEFAULT_THUMBNAIL_MATCH))

    @property
    def heartbeat(self) -> float:
        return float(self._option(CONF_HEARTBEAT, DEFAULT_HEARTBEAT))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Verify the token still works, then start listening."""
        await self.client.async_get_settings()

        players = self.players
        if players:
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, players, self._async_state_changed
                )
            )
        self._unsubscribers.append(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=TICK_INTERVAL)
            )
        )

        # Adopt anything that is already playing when we start up.
        for entity_id in players:
            if (state := self.hass.states.get(entity_id)) is not None:
                await self._async_evaluate(entity_id, state.state, state.attributes)

    async def async_shutdown(self, *_: Any) -> None:
        """Close out every open session so Trakt isn't left mid-watch."""
        self._shutting_down = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

        for entity_id in list(self.sessions):
            await self._async_end_session(entity_id, "shutdown", notify=False)

    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a sensor callback."""
        self._listeners.append(update)

        def remove() -> None:
            self._listeners.remove(update)

        return remove

    def _notify(self) -> None:
        for update in self._listeners:
            update()

    # ------------------------------------------------------------------
    # Player events
    # ------------------------------------------------------------------

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        entity_id = event.data["entity_id"]
        if new_state is None:
            await self._async_end_session(entity_id, "removed")
            return
        await self._async_evaluate(entity_id, new_state.state, new_state.attributes)

    async def _async_tick(self, _now: datetime) -> None:
        """Advance progress and refresh long-running scrobbles."""
        for entity_id in list(self.sessions):
            state = self.hass.states.get(entity_id)
            if state is None:
                await self._async_end_session(entity_id, "removed")
                continue
            await self._async_evaluate(entity_id, state.state, state.attributes)

    async def _async_evaluate(
        self, entity_id: str, state: str, attributes: dict[str, Any]
    ) -> None:
        if self._shutting_down:
            return
        async with self._lock:
            await self._evaluate(entity_id, state, attributes)
        self._notify()

    async def _evaluate(
        self, entity_id: str, state: str, attributes: dict[str, Any]
    ) -> None:
        if state in _DEAD_STATES or state not in _ACTIVE_STATES:
            await self._async_end_session(entity_id, state, notify=False)
            return

        skip = self._skip_reason(attributes)
        if skip is not None:
            await self._async_end_session(entity_id, skip, notify=False)
            self.sessions.pop(entity_id, None)
            self._ignored[entity_id] = skip
            return
        self._ignored.pop(entity_id, None)

        fingerprint = _fingerprint(attributes)
        if fingerprint is None:
            await self._async_end_session(entity_id, "no_media", notify=False)
            return

        session = self.sessions.get(entity_id)
        if session is not None and session.fingerprint != fingerprint:
            await self._async_end_session(entity_id, "changed", notify=False)
            session = None

        if session is None:
            session = ScrobbleSession(entity_id=entity_id, fingerprint=fingerprint)
            self.sessions[entity_id] = session
            session.update_playback(state, attributes)
            await self._async_identify(session, attributes)
        else:
            session.update_playback(state, attributes)

        await self._async_sync(session)

    def _skip_reason(self, attributes: dict[str, Any]) -> str | None:
        """Decide whether this content should be left alone entirely."""
        content_type = str(attributes.get("media_content_type") or "").lower()
        if content_type in IGNORED_CONTENT_TYPES:
            return f"content_type:{content_type}"

        haystacks = [
            str(attributes.get("app_name") or "").lower(),
            str(attributes.get("app_id") or "").lower(),
            str(attributes.get("source") or "").lower(),
        ]
        for excluded in self.excluded_apps:
            if any(excluded in haystack for haystack in haystacks if haystack):
                return f"excluded_app:{excluded}"

        duration = attributes.get("media_duration")
        if duration is not None:
            try:
                if float(duration) < self.min_duration:
                    return "too_short"
            except (TypeError, ValueError):
                pass
        return None

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    async def _async_identify(
        self, session: ScrobbleSession, attributes: dict[str, Any]
    ) -> None:
        parsed = parse_attributes(attributes)
        if parsed.item is None:
            session.status = STATE_UNMATCHED
            session.reason = parsed.reason or "unparsed"
            return

        session.item = parsed.item
        self._resolver.set_next_episode_fallback(self.next_episode_fallback)

        # For a bare show name (the Apple TV app), the player's artwork is the
        # same frame Trakt uses for the episode still, so hash it and let the
        # resolver identify the exact episode instead of guessing.
        thumbnail_hash = None
        if self.thumbnail_match and parsed.item.kind in (KIND_SHOW, "ambiguous"):
            thumbnail_hash = await self._async_thumbnail_hash(attributes)

        try:
            session.resolved = await self._resolver.async_resolve(
                parsed.item, session.duration, thumbnail_hash
            )
        except ResolutionError as err:
            session.status = STATE_UNMATCHED
            session.reason = err.reason
            session.error = str(err)
            _LOGGER.debug("Could not resolve %s: %s", parsed.item.slug, err)
            return
        except TraktAuthError as err:
            self._handle_auth_error(err)
            session.status = STATE_ERROR
            session.error = str(err)
            return

        session.reason = None
        session.error = None
        _LOGGER.info(
            "%s -> %s (%s)",
            parsed.item.raw_title or parsed.item.slug,
            session.resolved.display,
            session.resolved.method,
        )

    async def _async_thumbnail_hash(self, attributes: dict[str, Any]) -> int | None:
        """Fetch the player's artwork and return its difference hash."""
        picture = attributes.get("entity_picture")
        if not picture or not image_match.available():
            return None
        try:
            url = picture if picture.startswith("http") else f"{get_url(self.hass, allow_internal=True)}{picture}"
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=15) as response:
                if response.status != 200:
                    return None
                data = await response.read()
        except Exception as err:  # noqa: BLE001 - artwork is best-effort
            _LOGGER.debug("Could not fetch player artwork: %s", err)
            return None
        return image_match.hash_bytes(data)

    # ------------------------------------------------------------------
    # Scrobbling
    # ------------------------------------------------------------------

    async def _async_sync(self, session: ScrobbleSession) -> None:
        """Send whatever scrobble action the current state calls for."""
        if session.resolved is None:
            return
        if not session.duration:
            session.status = STATE_UNMATCHED
            session.reason = "no_duration"
            return

        if session.player_state == STATE_PLAYING:
            action = ACTION_START
        elif session.player_state == STATE_PAUSED:
            action = ACTION_PAUSE
        else:  # buffering: hold whatever we last told Trakt
            return

        progress = session.progress
        if session.last_action == action:
            # Same action as last time: only re-send as a periodic heartbeat so
            # Trakt's progress bar keeps up with seeks.
            if action != ACTION_START:
                return
            if session.last_sent_at is None:
                return
            age = (dt_util.utcnow() - session.last_sent_at).total_seconds()
            moved = abs(progress - (session.last_sent_progress or 0))
            if age < self.heartbeat or moved < _PROGRESS_EPSILON:
                return

        await self._async_send(session, action, progress)

    async def _async_end_session(
        self, entity_id: str, reason: str, notify: bool = True
    ) -> None:
        """Stop the scrobble for ``entity_id``, if there is one."""
        session = self.sessions.pop(entity_id, None)
        if session is None:
            return
        if session.resolved is not None and session.last_action in (
            ACTION_START,
            ACTION_PAUSE,
        ):
            await self._async_send(session, ACTION_STOP, session.progress)
            _LOGGER.info(
                "Stopped scrobble for %s at %.1f%% (%s)",
                session.resolved.display,
                session.progress,
                reason,
            )
        if notify:
            self._notify()

    async def _async_send(
        self, session: ScrobbleSession, action: str, progress: float
    ) -> None:
        assert session.resolved is not None
        payload = dict(session.resolved.payload)
        payload["progress"] = round(progress, 2)

        session.last_action = action
        session.last_sent_progress = progress
        session.last_sent_at = dt_util.utcnow()
        if action == ACTION_START:
            session.status = STATE_WATCHING
        elif action == ACTION_PAUSE:
            session.status = STATE_PAUSED
        else:
            session.status = STATE_IDLE

        try:
            await self.client.async_scrobble(action, payload)
            self.last_error = None
            session.error = None
        except TraktRateLimitError as err:
            # Not fatal -- the next tick will try again.
            _LOGGER.warning("Trakt rate limited the %s scrobble: %s", action, err)
            session.last_sent_at = None
            self.last_error = str(err)
        except TraktAuthError as err:
            self._handle_auth_error(err)
            session.status = STATE_ERROR
            session.error = str(err)
        except TraktError as err:
            _LOGGER.error(
                "Failed to scrobble %s for %s: %s", action, session.resolved.display, err
            )
            session.status = STATE_ERROR
            session.error = str(err)
            self.last_error = str(err)

    def _handle_auth_error(self, err: Exception) -> None:
        _LOGGER.error("Trakt authorization failed: %s", err)
        self.last_error = str(err)
        self.entry.async_start_reauth(self.hass)

    # ------------------------------------------------------------------
    # Service helpers
    # ------------------------------------------------------------------

    def ignored_reason(self, entity_id: str) -> str | None:
        return self._ignored.get(entity_id)

    async def async_preview_resolve(
        self, item: MediaItem, duration: float | None
    ) -> Resolved:
        """Resolve an item the way a real session would, for the preview service."""
        self._resolver.set_next_episode_fallback(self.next_episode_fallback)
        return await self._resolver.async_resolve(item, duration)

    async def async_force_stop(self, entity_id: str) -> bool:
        """Service entry point: end a session early."""
        if entity_id not in self.sessions:
            return False
        async with self._lock:
            await self._async_end_session(entity_id, "service_call", notify=False)
        self._notify()
        return True

    async def async_save_tokens(self, tokens: Tokens) -> None:
        """Persist refreshed tokens back into the config entry."""
        from .const import CONF_TOKENS

        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_TOKENS: tokens.as_dict()},
        )


def _fingerprint(attributes: dict[str, Any]) -> tuple | None:
    """Identity of what is on screen, so we notice when it changes."""
    parts = (
        attributes.get("app_id"),
        attributes.get("media_content_id"),
        attributes.get("media_title"),
        attributes.get("media_series_title"),
        attributes.get("media_season"),
        attributes.get("media_episode"),
    )
    if not any(parts[1:]):
        return None
    return parts
