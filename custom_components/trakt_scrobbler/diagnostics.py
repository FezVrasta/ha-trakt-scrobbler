"""Diagnostics for Trakt Scrobbler."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    BUNDLED_CLIENT_ID,
    CONF_AUTH_MODE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKENS,
)

if TYPE_CHECKING:
    from . import TraktScrobblerConfigEntry

TO_REDACT = {CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_TOKENS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: "TraktScrobblerConfigEntry"
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    manager = entry.runtime_data

    sessions = {}
    for entity_id, session in manager.sessions.items():
        sessions[entity_id] = {
            "status": session.status,
            "progress": round(session.progress, 1),
            "duration": session.duration,
            "last_action": session.last_action,
            "reason": session.reason,
            "error": session.error,
            "detected": session.item.slug if session.item else None,
            "parse_method": session.item.method if session.item else None,
            "raw_title": session.item.raw_title if session.item else None,
            "trakt": session.resolved.display if session.resolved else None,
            "match_method": session.resolved.method if session.resolved else None,
            "guessed": session.resolved.guessed if session.resolved else None,
        }

    players = {}
    for entity_id in manager.players:
        state = hass.states.get(entity_id)
        players[entity_id] = {
            "available": state is not None,
            "state": state.state if state else None,
            "attributes": {
                key: value
                for key, value in (state.attributes.items() if state else [])
                if key.startswith(("media_", "app_")) or key == "source"
            },
            "ignored_reason": manager.ignored_reason(entity_id),
        }

    client_id = entry.data.get(CONF_CLIENT_ID, BUNDLED_CLIENT_ID)
    return {
        "auth_mode": entry.data.get(CONF_AUTH_MODE),
        "client_id_prefix": f"{client_id[:8]}…",
        "client_id_is_bundled_seed": client_id == BUNDLED_CLIENT_ID,
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "players": players,
        "sessions": sessions,
        "last_error": manager.last_error,
    }
