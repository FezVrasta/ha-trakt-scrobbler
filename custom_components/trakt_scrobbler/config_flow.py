"""Config and options flows for Trakt Scrobbler."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DeviceCode,
    DeviceCodeDenied,
    DeviceCodeExpired,
    Tokens,
    TraktAuthError,
    TraktClient,
    TraktError,
)
from .discovery import async_resolve_client_id
from .const import (
    API_BASE,
    API_BASE_PRIVATE,
    AUTH_MODE_BUNDLED,
    AUTH_MODE_OWN_APP,
    BUNDLED_CLIENT_ID,
    CONF_API_BASE,
    CONF_AUTH_MODE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXCLUDED_APPS,
    CONF_HEARTBEAT,
    CONF_MIN_DURATION,
    CONF_NEXT_EPISODE_FALLBACK,
    CONF_THUMBNAIL_MATCH,
    CONF_PLAYERS,
    CONF_TOKENS,
    CONF_USERNAME,
    DEFAULT_EXCLUDED_APPS,
    DEFAULT_HEARTBEAT,
    DEFAULT_MIN_DURATION,
    DEFAULT_NEXT_EPISODE_FALLBACK,
    DEFAULT_THUMBNAIL_MATCH,
    DOMAIN,
    TRAKT_APP_URL,
)

_LOGGER = logging.getLogger(__name__)

_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): selector.TextSelector(),
        vol.Required(CONF_CLIENT_SECRET): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

_PLAYER_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="media_player", multiple=True)
)


def _options_schema(options: dict[str, Any]) -> vol.Schema:
    """Build the options form, pre-filled with the current values."""
    return vol.Schema(
        {
            vol.Required(
                CONF_PLAYERS, default=options.get(CONF_PLAYERS, [])
            ): _PLAYER_SELECTOR,
            vol.Optional(
                CONF_EXCLUDED_APPS,
                default=options.get(CONF_EXCLUDED_APPS, DEFAULT_EXCLUDED_APPS),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[], multiple=True, custom_value=True
                )
            ),
            vol.Optional(
                CONF_MIN_DURATION,
                default=options.get(CONF_MIN_DURATION, DEFAULT_MIN_DURATION),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=7200,
                    step=30,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_HEARTBEAT,
                default=options.get(CONF_HEARTBEAT, DEFAULT_HEARTBEAT),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=60,
                    max=3600,
                    step=30,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_NEXT_EPISODE_FALLBACK,
                default=options.get(
                    CONF_NEXT_EPISODE_FALLBACK, DEFAULT_NEXT_EPISODE_FALLBACK
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_THUMBNAIL_MATCH,
                default=options.get(CONF_THUMBNAIL_MATCH, DEFAULT_THUMBNAIL_MATCH),
            ): selector.BooleanSelector(),
        }
    )


class TraktScrobblerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect Trakt app credentials, then run the device authorization flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._auth_mode: str = AUTH_MODE_BUNDLED
        self._api_base: str = API_BASE_PRIVATE
        self._client: TraktClient | None = None
        self._device: DeviceCode | None = None
        self._task: asyncio.Task[tuple[Tokens, str]] | None = None
        self._tokens: Tokens | None = None
        self._username: str | None = None
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the no-API-app path first, with bring-your-own as the alternative."""
        return self.async_show_menu(
            step_id="user", menu_options=["bundled", "own_app"]
        )

    async def async_step_bundled(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authorize using Trakt's own public web client -- no app registration."""
        session = async_get_clientsession(self.hass)
        # Read the current id from the live web app, falling back to the seed.
        self._client_id, _ = await async_resolve_client_id(session)
        self._client_secret = None
        self._api_base = API_BASE_PRIVATE
        self._auth_mode = AUTH_MODE_BUNDLED
        self._client = TraktClient(
            session, self._client_id, api_base=self._api_base
        )
        try:
            self._device = await self._client.async_request_device_code()
        except TraktError as err:
            _LOGGER.debug("Bundled-client device code request failed: %s", err)
            self._errors["base"] = "cannot_connect"
            return await self.async_step_own_app()
        return await self.async_step_device()

    async def async_step_own_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors, self._errors = self._errors, {}
        self._auth_mode = AUTH_MODE_OWN_APP
        self._api_base = API_BASE

        if user_input is not None:
            self._client_id = user_input[CONF_CLIENT_ID].strip()
            self._client_secret = user_input[CONF_CLIENT_SECRET].strip()
            self._client = TraktClient(
                async_get_clientsession(self.hass),
                self._client_id,
                self._client_secret,
                api_base=self._api_base,
            )
            try:
                self._device = await self._client.async_request_device_code()
            except TraktAuthError:
                errors["base"] = "invalid_credentials"
            except TraktError as err:
                _LOGGER.debug("Device code request failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_device()

        return self.async_show_form(
            step_id="own_app",
            data_schema=self.add_suggested_values_to_schema(
                _CREDENTIALS_SCHEMA, user_input or {}
            ),
            errors=errors,
            description_placeholders={"app_url": TRAKT_APP_URL},
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the user to approve the code on trakt.tv."""
        assert self._client is not None and self._device is not None

        if self._task is None:
            self._task = self.hass.async_create_task(self._async_authorize())

        if not self._task.done():
            return self.async_show_progress(
                step_id="device",
                progress_action="wait_for_authorization",
                progress_task=self._task,
                description_placeholders={
                    "url": self._device.verification_url,
                    "code": self._device.user_code,
                },
            )

        try:
            self._tokens, self._username = self._task.result()
        except DeviceCodeExpired:
            self._errors["base"] = "code_expired"
        except DeviceCodeDenied:
            self._errors["base"] = "authorization_denied"
        except TraktAuthError:
            self._errors["base"] = "invalid_credentials"
        except TraktError as err:
            _LOGGER.debug("Device authorization failed: %s", err)
            self._errors["base"] = "cannot_connect"

        self._task = None
        self._device = None

        if self._errors:
            return self.async_show_progress_done(next_step_id="retry")
        if self.source == SOURCE_REAUTH:
            return self.async_show_progress_done(next_step_id="reauth_finish")
        return self.async_show_progress_done(next_step_id="players")

    async def _async_authorize(self) -> tuple[Tokens, str]:
        """Poll Trakt until the code is approved, then identify the account."""
        assert self._client is not None and self._device is not None
        tokens = await self._client.async_poll_device_token(self._device)
        settings = await self._client.async_get_settings()
        username = (
            settings.get("user", {}).get("username")
            or settings.get("user", {}).get("ids", {}).get("slug")
            or "Trakt"
        )
        return tokens, username

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authorization failed -- send the user back to the form they came from."""
        if self.source == SOURCE_REAUTH:
            return await self.async_step_reauth_confirm()
        # A bundled-key failure falls through to the bring-your-own-app form,
        # which is the only thing the user can actually act on.
        return await self.async_step_own_app()

    async def async_step_players(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which media players feed the scrobbler."""
        if user_input is not None:
            assert self._tokens is not None
            await self.async_set_unique_id(str(self._username).lower())
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Trakt ({self._username})",
                data={
                    CONF_AUTH_MODE: self._auth_mode,
                    CONF_API_BASE: self._api_base,
                    CONF_CLIENT_ID: self._client_id,
                    CONF_CLIENT_SECRET: self._client_secret,
                    CONF_USERNAME: self._username,
                    CONF_TOKENS: self._tokens.as_dict(),
                },
                options={
                    CONF_PLAYERS: user_input[CONF_PLAYERS],
                    CONF_EXCLUDED_APPS: DEFAULT_EXCLUDED_APPS,
                    CONF_MIN_DURATION: DEFAULT_MIN_DURATION,
                    CONF_HEARTBEAT: DEFAULT_HEARTBEAT,
                    CONF_NEXT_EPISODE_FALLBACK: DEFAULT_NEXT_EPISODE_FALLBACK,
                    CONF_THUMBNAIL_MATCH: DEFAULT_THUMBNAIL_MATCH,
                },
            )

        return self.async_show_form(
            step_id="players",
            data_schema=vol.Schema({vol.Required(CONF_PLAYERS): _PLAYER_SELECTOR}),
            description_placeholders={"username": str(self._username)},
        )

    # ------------------------------------------------------------------
    # Reauth
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._client_id = entry_data.get(CONF_CLIENT_ID, BUNDLED_CLIENT_ID)
        self._client_secret = entry_data.get(CONF_CLIENT_SECRET)
        self._auth_mode = entry_data.get(CONF_AUTH_MODE, AUTH_MODE_BUNDLED)
        self._api_base = entry_data.get(CONF_API_BASE, API_BASE_PRIVATE)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors, self._errors = self._errors, {}

        if user_input is not None:
            self._client = TraktClient(
                async_get_clientsession(self.hass),
                str(self._client_id),
                self._client_secret,
                api_base=self._api_base,
            )
            try:
                self._device = await self._client.async_request_device_code()
            except TraktError as err:
                _LOGGER.debug("Reauth device code request failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_device()

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_reauth_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Write the freshly issued tokens back into the existing entry."""
        assert self._tokens is not None
        entry = self._get_reauth_entry()
        return self.async_update_reload_and_abort(
            entry,
            data={**entry.data, CONF_TOKENS: self._tokens.as_dict()},
            reason="reauth_successful",
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return TraktScrobblerOptionsFlow()


class TraktScrobblerOptionsFlow(OptionsFlow):
    """Tune which players are watched and how strict the filters are."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_MIN_DURATION] = int(user_input[CONF_MIN_DURATION])
            user_input[CONF_HEARTBEAT] = int(user_input[CONF_HEARTBEAT])
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self.config_entry.options))
        )
