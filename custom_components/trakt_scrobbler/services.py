"""Services for inspecting and nudging the scrobbler."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import TraktError
from .const import (
    DOMAIN,
    SERVICE_PARSE_PREVIEW,
    SERVICE_REFRESH_TOKEN,
    SERVICE_STOP_SCROBBLE,
)
from .parser import parse_attributes
from .resolver import ResolutionError

_LOGGER = logging.getLogger(__name__)

ATTR_PLAYER = "player"
ATTR_TITLE = "title"
ATTR_CONTENT_TYPE = "media_content_type"
ATTR_DURATION = "media_duration"
ATTR_RESOLVE = "resolve"

_PARSE_PREVIEW_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_PLAYER): cv.entity_id,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_CONTENT_TYPE): cv.string,
        vol.Optional(ATTR_DURATION): vol.Coerce(float),
        vol.Optional(ATTR_RESOLVE, default=True): cv.boolean,
    }
)

_STOP_SCROBBLE_SCHEMA = vol.Schema({vol.Required(ATTR_PLAYER): cv.entity_id})


def _managers(hass: HomeAssistant) -> list[Any]:
    """Every loaded entry's scrobble manager."""
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services, once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, SERVICE_PARSE_PREVIEW):
        return

    async def async_parse_preview(call: ServiceCall) -> ServiceResponse:
        """Show how a title would be parsed and matched, without scrobbling."""
        attributes: dict[str, Any] = {}

        if player := call.data.get(ATTR_PLAYER):
            state = hass.states.get(player)
            if state is None:
                raise ServiceValidationError(f"Unknown entity: {player}")
            attributes = dict(state.attributes)

        if title := call.data.get(ATTR_TITLE):
            attributes["media_title"] = title
        if content_type := call.data.get(ATTR_CONTENT_TYPE):
            attributes["media_content_type"] = content_type
        if (duration := call.data.get(ATTR_DURATION)) is not None:
            attributes["media_duration"] = duration

        if not attributes:
            raise ServiceValidationError(
                f"Pass either '{ATTR_PLAYER}' or '{ATTR_TITLE}'"
            )

        parsed = parse_attributes(attributes)
        response: dict[str, Any] = {
            "input": {
                "media_title": attributes.get("media_title"),
                "media_series_title": attributes.get("media_series_title"),
                "media_content_type": attributes.get("media_content_type"),
                "media_duration": attributes.get("media_duration"),
                "app_name": attributes.get("app_name"),
            },
            "parsed": None,
            "resolved": None,
        }

        if parsed.item is None:
            response["reason"] = parsed.reason
            return response

        item = parsed.item
        response["parsed"] = {
            "kind": item.kind,
            "title": item.title,
            "year": item.year,
            "season": item.season,
            "episode": item.episode,
            "episode_title": item.episode_title,
            "method": item.method,
            "label": item.slug,
        }

        if not call.data.get(ATTR_RESOLVE, True):
            return response

        managers = _managers(hass)
        if not managers:
            response["reason"] = "no_loaded_entry"
            return response

        manager = managers[0]
        duration = attributes.get("media_duration")
        try:
            resolved = await manager.async_preview_resolve(
                item, float(duration) if duration else None
            )
        except ResolutionError as err:
            response["reason"] = err.reason
            response["error"] = str(err)
            return response
        except TraktError as err:
            raise HomeAssistantError(f"Trakt lookup failed: {err}") from err

        response["resolved"] = {
            "kind": resolved.kind,
            "display": resolved.display,
            "trakt_id": resolved.trakt_id,
            "show_id": resolved.show_id,
            "season": resolved.season,
            "episode": resolved.episode,
            "url": resolved.url,
            "method": resolved.method,
            "guessed": resolved.guessed,
            "scrobble_payload": resolved.payload,
        }
        return response

    async def async_stop_scrobble(call: ServiceCall) -> None:
        """End an in-flight scrobble early, submitting its current progress."""
        player = call.data[ATTR_PLAYER]
        for manager in _managers(hass):
            if await manager.async_force_stop(player):
                return
        raise ServiceValidationError(f"No active scrobble for {player}")

    async def async_refresh_token(_call: ServiceCall) -> None:
        """Force an OAuth token refresh."""
        managers = _managers(hass)
        if not managers:
            raise ServiceValidationError("Trakt Scrobbler is not set up")
        for manager in managers:
            try:
                await manager.client.async_refresh_token(force=True)
            except TraktError as err:
                raise HomeAssistantError(f"Token refresh failed: {err}") from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_PARSE_PREVIEW,
        async_parse_preview,
        schema=_PARSE_PREVIEW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_SCROBBLE, async_stop_scrobble, schema=_STOP_SCROBBLE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_TOKEN, async_refresh_token, schema=vol.Schema({})
    )
