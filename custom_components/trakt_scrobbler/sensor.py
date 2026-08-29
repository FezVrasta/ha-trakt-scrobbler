"""A sensor per watched player, showing what is being scrobbled."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_USERNAME,
    DOMAIN,
    STATE_ERROR,
    STATE_IDLE,
    STATE_IGNORED,
    STATE_PAUSED,
    STATE_UNMATCHED,
    STATE_WATCHING,
)

if TYPE_CHECKING:
    from . import TraktScrobblerConfigEntry
    from .coordinator import ScrobbleManager

_OPTIONS = [
    STATE_IDLE,
    STATE_WATCHING,
    STATE_PAUSED,
    STATE_UNMATCHED,
    STATE_IGNORED,
    STATE_ERROR,
]


def _hms(seconds: int) -> str:
    """Format seconds as M:SS, or H:MM:SS past an hour."""
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "TraktScrobblerConfigEntry",
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one scrobble sensor per configured media player."""
    manager = entry.runtime_data
    async_add_entities(
        ScrobbleStatusSensor(manager, entry, player) for player in manager.players
    )


class ScrobbleStatusSensor(SensorEntity):
    """Reports what the scrobbler is currently doing for one player."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _OPTIONS
    _attr_icon = "mdi:filmstrip"

    def __init__(
        self,
        manager: "ScrobbleManager",
        entry: "TraktScrobblerConfigEntry",
        player: str,
    ) -> None:
        self._manager = manager
        self._player = player
        self._attr_unique_id = f"{entry.entry_id}_{player}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Trakt ({entry.data.get(CONF_USERNAME, 'account')})",
            manufacturer="Trakt",
            model="Scrobbler",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://trakt.tv",
        )

    @property
    def name(self) -> str:
        """Name the sensor after the player it follows."""
        state = self.hass.states.get(self._player)
        friendly = (
            state.attributes.get("friendly_name") if state is not None else None
        )
        return friendly or self._player.split(".", 1)[-1].replace("_", " ").title()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        session = self._manager.sessions.get(self._player)
        if session is None:
            return STATE_IGNORED if self._manager.ignored_reason(self._player) else STATE_IDLE
        return session.status

    @property
    def entity_picture(self) -> str | None:
        """Show the episode still on the sensor, when there is one."""
        session = self._manager.sessions.get(self._player)
        return session.artwork if session is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {"player": self._player}

        if reason := self._manager.ignored_reason(self._player):
            attributes["ignored_reason"] = reason

        session = self._manager.sessions.get(self._player)
        if session is None:
            return attributes

        elapsed = round(session.elapsed)
        duration = int(session.duration) if session.duration else None
        remaining = max(duration - elapsed, 0) if duration is not None else None
        attributes.update(
            {
                "player_state": session.player_state,
                "progress": round(session.progress, 1),
                "position": _hms(elapsed),
                "position_seconds": elapsed,
                "duration": _hms(duration) if duration is not None else None,
                "duration_seconds": duration,
                "remaining": _hms(remaining) if remaining is not None else None,
                "remaining_seconds": remaining,
                "last_action": session.last_action,
                "session_started": session.started_at.isoformat(),
                "artwork": session.artwork,
            }
        )

        if session.item is not None:
            attributes["detected"] = session.item.slug
            attributes["parse_method"] = session.item.method
            attributes["raw_title"] = session.item.raw_title

        if session.resolved is not None:
            resolved = session.resolved
            attributes.update(
                {
                    "media_type": resolved.kind,
                    "trakt_title": resolved.display,
                    "trakt_url": resolved.url,
                    "trakt_id": resolved.trakt_id,
                    "match_method": resolved.method,
                    "episode_guessed": resolved.guessed,
                    "show_title": resolved.extra.get("show_title"),
                    "episode_title": resolved.extra.get("episode_title"),
                }
            )
            # A single friendly title regardless of movie vs episode.
            attributes["title"] = (
                resolved.extra.get("episode_title")
                if resolved.kind == "episode"
                else resolved.display
            )
            if resolved.season is not None:
                attributes["season"] = resolved.season
                attributes["episode"] = resolved.episode
                attributes["episode_code"] = f"S{resolved.season:02d}E{resolved.episode:02d}"

        if session.reason:
            attributes["reason"] = session.reason
        if session.error:
            attributes["error"] = session.error

        return attributes
