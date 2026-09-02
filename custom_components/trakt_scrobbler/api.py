"""Thin async client for the Trakt API.

Only the endpoints the scrobbler needs: device OAuth, search, show progress and
the three scrobble calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import API_BASE

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = 20
#: refresh the access token this long before it actually expires
_REFRESH_MARGIN = 24 * 60 * 60


class TraktError(Exception):
    """Any failure talking to Trakt."""


class TraktAuthError(TraktError):
    """Credentials are missing, rejected or no longer refreshable."""


class TraktRateLimitError(TraktError):
    """Trakt asked us to back off."""


class TraktNotFoundError(TraktError):
    """Trakt has no such item."""


class TraktAlreadyScrobbledError(TraktError):
    """Trakt has already recorded this item as watched.

    Returned as HTTP 409 when the same episode is scrobbled twice in quick
    succession -- which happens routinely, because a player often keeps
    reporting an episode on its post-play screen after it has finished.
    """


class DeviceCodeExpired(TraktError):
    """The device code ran out before the user approved it."""


class DeviceCodeDenied(TraktError):
    """The user rejected the authorization request."""


@dataclass(slots=True)
class DeviceCode:
    """Response of ``POST /oauth/device/code``."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(slots=True)
class Tokens:
    """OAuth tokens plus the absolute time the access token dies."""

    access_token: str
    refresh_token: str
    expires_at: float

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> "Tokens":
        created = payload.get("created_at") or time.time()
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            expires_at=float(created) + float(payload.get("expires_in", 7776000)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data.get("expires_at", 0)),
        )

    @property
    def needs_refresh(self) -> bool:
        return time.time() >= self.expires_at - _REFRESH_MARGIN


class TraktClient:
    """Talks to Trakt, keeping the access token fresh along the way."""

    def __init__(
        self,
        session: ClientSession,
        client_id: str,
        client_secret: str | None = None,
        tokens: Tokens | None = None,
        token_saver: Callable[[Tokens], Awaitable[None]] | None = None,
        api_base: str = API_BASE,
    ) -> None:
        self._session = session
        self._client_id = client_id
        # None when running as a public client (the bundled key); Trakt's token
        # endpoint accepts `none` as an auth method, so the secret is omitted.
        self._client_secret = client_secret
        self._api_base = api_base
        self._tokens = tokens
        self._token_saver = token_saver
        self._refresh_lock = asyncio.Lock()

    @property
    def session(self) -> ClientSession:
        return self._session

    @property
    def tokens(self) -> Tokens | None:
        return self._tokens

    @property
    def is_authorized(self) -> bool:
        return self._tokens is not None

    # ------------------------------------------------------------------
    # Device OAuth
    # ------------------------------------------------------------------

    async def async_request_device_code(self) -> DeviceCode:
        """Kick off the device flow and get a code for the user to type in."""
        payload = await self._post_json(
            "/oauth/device/code", {"client_id": self._client_id}, authed=False
        )
        return DeviceCode(
            device_code=payload["device_code"],
            user_code=payload["user_code"],
            verification_url=payload["verification_url"],
            expires_in=int(payload.get("expires_in", 600)),
            interval=int(payload.get("interval", 5)),
        )

    async def async_poll_device_token(self, device: DeviceCode) -> Tokens:
        """Poll until the user approves the code (or it expires)."""
        interval = max(device.interval, 1)
        deadline = time.time() + device.expires_in

        while time.time() < deadline:
            await asyncio.sleep(interval)
            try:
                async with self._session.post(
                    f"{self._api_base}/oauth/device/token",
                    json=self._with_secret({
                        "code": device.device_code,
                        "client_id": self._client_id,
                    }),
                    timeout=_TIMEOUT,
                ) as response:
                    if response.status == 200:
                        tokens = Tokens.from_response(await response.json())
                        await self._store(tokens)
                        return tokens
                    if response.status == 400:  # pending, keep waiting
                        continue
                    if response.status == 429:  # slow down
                        interval += 1
                        continue
                    if response.status == 404:
                        raise TraktAuthError("Trakt rejected the device code")
                    if response.status == 409:
                        raise TraktAuthError("This device code was already used")
                    if response.status == 410:
                        raise DeviceCodeExpired("The device code expired")
                    if response.status == 418:
                        raise DeviceCodeDenied("Authorization was denied")
                    raise TraktError(
                        f"Unexpected status {response.status} while polling for a token"
                    )
            except ClientError as err:
                raise TraktError(f"Network error while polling Trakt: {err}") from err

        raise DeviceCodeExpired("The device code expired")

    async def async_refresh_token(self, force: bool = False) -> Tokens:
        """Swap the refresh token for a new access token."""
        if self._tokens is None:
            raise TraktAuthError("No refresh token available")

        async with self._refresh_lock:
            # Another caller may have refreshed while we waited for the lock.
            if not force and not self._tokens.needs_refresh:
                return self._tokens

            payload = await self._post_json(
                "/oauth/token",
                self._with_secret({
                    "refresh_token": self._tokens.refresh_token,
                    "client_id": self._client_id,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                    "grant_type": "refresh_token",
                }),
                authed=False,
            )
            tokens = Tokens.from_response(payload)
            await self._store(tokens)
            return tokens

    def _with_secret(self, body: dict[str, Any]) -> dict[str, Any]:
        """Add client_secret only when this client actually has one."""
        if self._client_secret:
            return {**body, "client_secret": self._client_secret}
        return body

    async def _store(self, tokens: Tokens) -> None:
        self._tokens = tokens
        if self._token_saver is not None:
            await self._token_saver(tokens)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def async_get_settings(self) -> dict[str, Any]:
        """Fetch the signed-in user, used to label the config entry."""
        return await self._request("GET", "/users/settings")

    async def async_search(
        self, kind: str, query: str, year: int | None = None
    ) -> list[dict[str, Any]]:
        """Text search, most relevant first. ``kind`` is ``movie`` or ``show``."""
        params: dict[str, Any] = {"query": query, "limit": 5, "extended": "full"}
        if year:
            params["years"] = str(year)
        result = await self._request(
            "GET", f"/search/{kind}", params=params, require_auth=False
        )
        return result if isinstance(result, list) else []

    async def async_get_episode(
        self, show_id: int, season: int, episode: int
    ) -> dict[str, Any]:
        """Look up a single episode, confirming it exists before scrobbling."""
        return await self._request(
            "GET",
            f"/shows/{show_id}/seasons/{season}/episodes/{episode}",
            params={"extended": "full"},
            require_auth=False,
        )

    async def async_get_seasons(self, show_id: int) -> list[dict[str, Any]]:
        """List a show's seasons (numbers only needed)."""
        result = await self._request(
            "GET", f"/shows/{show_id}/seasons", require_auth=False
        )
        return result if isinstance(result, list) else []

    async def async_get_season_episodes(
        self, show_id: int, season: int
    ) -> list[dict[str, Any]]:
        """All episodes of a season, each with its screenshot image."""
        result = await self._request(
            "GET",
            f"/shows/{show_id}/seasons/{season}",
            params={"extended": "full,images"},
            require_auth=False,
        )
        return result if isinstance(result, list) else []

    async def async_get_watched_progress(self, show_id: int) -> dict[str, Any]:
        """Watched progress for a show, which carries ``next_episode``."""
        return await self._request(
            "GET",
            f"/shows/{show_id}/progress/watched",
            params={"hidden": "false", "specials": "false", "count_specials": "false"},
        )

    async def async_scrobble(
        self, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST to ``/scrobble/{start,pause,stop}``."""
        return await self._request("POST", f"/scrobble/{action}", json=payload)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _headers(self, authed: bool = True) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        }
        if authed and self._tokens is not None:
            headers["Authorization"] = f"Bearer {self._tokens.access_token}"
        return headers

    async def _post_json(
        self, path: str, body: dict[str, Any], authed: bool = True
    ) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{self._api_base}{path}",
                json=body,
                headers=self._headers(authed),
                timeout=_TIMEOUT,
            ) as response:
                return await self._handle(response)
        except ClientError as err:
            raise TraktError(f"Network error talking to Trakt: {err}") from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        require_auth: bool = True,
        _retry: bool = True,
    ) -> Any:
        # Search and catalogue lookups are public: the API key alone is enough,
        # so matching still works before the user has authorised anything.
        if self._tokens is None:
            if require_auth:
                raise TraktAuthError("Not authorized with Trakt")
        elif self._tokens.needs_refresh:
            await self.async_refresh_token()

        try:
            async with self._session.request(
                method,
                f"{self._api_base}{path}",
                params=params,
                json=json,
                headers=self._headers(),
                timeout=_TIMEOUT,
            ) as response:
                if response.status == 401 and _retry and self._tokens is not None:
                    # Token died early; refresh once and try again.
                    self._tokens.expires_at = 0
                    await self.async_refresh_token()
                    return await self._request(
                        method,
                        path,
                        params=params,
                        json=json,
                        require_auth=require_auth,
                        _retry=False,
                    )
                return await self._handle(response)
        except ClientError as err:
            raise TraktError(f"Network error talking to Trakt: {err}") from err

    async def _handle(self, response: ClientResponse) -> Any:
        if response.status in (200, 201):
            return await response.json()
        if response.status == 204:
            return {}
        if response.status in (401, 403):
            raise TraktAuthError(
                f"Trakt rejected our credentials (HTTP {response.status})"
            )
        if response.status == 404:
            raise TraktNotFoundError(f"Not found: {response.url.path}")
        if response.status == 409:
            # The body carries when the existing scrobble landed.
            raise TraktAlreadyScrobbledError(
                f"Already scrobbled: {(await response.text())[:200]}"
            )
        if response.status == 429:
            retry_after = response.headers.get("Retry-After", "1")
            raise TraktRateLimitError(f"Rate limited, retry after {retry_after}s")
        body = await response.text()
        raise TraktError(f"HTTP {response.status} from {response.url.path}: {body[:200]}")
