"""The Trakt Scrobbler integration.

Turns what a Home Assistant media player reports into Trakt scrobbles, so
anything you watch on an Apple TV (or Plex, Jellyfin, Kodi...) lands in your
Trakt history without a second app running on the device.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Tokens, TraktAuthError, TraktClient, TraktError
from .const import (
    API_BASE_PRIVATE,
    AUTH_MODE_BUNDLED,
    BUNDLED_CLIENT_ID,
    CONF_API_BASE,
    CONF_AUTH_MODE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKENS,
)
from .coordinator import ScrobbleManager
from .discovery import async_resolve_client_id
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type TraktScrobblerConfigEntry = ConfigEntry[ScrobbleManager]


async def async_setup_entry(
    hass: HomeAssistant, entry: TraktScrobblerConfigEntry
) -> bool:
    """Set up Trakt Scrobbler from a config entry."""

    async def save_tokens(tokens: Tokens) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKENS: tokens.as_dict()}
        )

    session = async_get_clientsession(hass)
    client_id = entry.data.get(CONF_CLIENT_ID, BUNDLED_CLIENT_ID)
    api_base = entry.data.get(CONF_API_BASE, API_BASE_PRIVATE)

    # On the shared-client path the id belongs to Trakt and can be rotated out
    # from under us. Confirm it still works and pick up the current one if not.
    if entry.data.get(CONF_AUTH_MODE, AUTH_MODE_BUNDLED) == AUTH_MODE_BUNDLED:
        client_id, rediscovered = await async_resolve_client_id(session, client_id)
        if rediscovered:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_CLIENT_ID: client_id}
            )

    client = TraktClient(
        session,
        client_id,
        entry.data.get(CONF_CLIENT_SECRET),
        tokens=Tokens.from_dict(entry.data[CONF_TOKENS]),
        token_saver=save_tokens,
        api_base=api_base,
    )

    manager = ScrobbleManager(hass, entry, client)
    try:
        await manager.async_setup()
    except TraktAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TraktError as err:
        raise ConfigEntryNotReady(f"Cannot reach Trakt: {err}") from err

    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Close open scrobbles when Home Assistant itself goes down.
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, manager.async_shutdown)
    )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    async_register_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: TraktScrobblerConfigEntry
) -> bool:
    """Unload a config entry, stopping any scrobble still in flight."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_reload_entry(
    hass: HomeAssistant, entry: TraktScrobblerConfigEntry
) -> None:
    """Re-subscribe after the player list or filters changed."""
    await hass.config_entries.async_reload(entry.entry_id)
