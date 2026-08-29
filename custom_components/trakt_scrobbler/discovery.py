"""Find the public client id that Trakt's own web app uses.

The id is a plain string literal in the JavaScript that `app.trakt.tv` serves to
every visitor -- it is an OAuth *public client identifier*, not a secret. We
still prefer to read it from the live bundle rather than trust a value frozen
into this repo, so that a rotation on Trakt's side heals itself instead of
breaking every install.

Cost is kept near zero in the normal case: :func:`async_resolve_client_id`
validates the id we already have with one small request and only falls through
to :func:`async_discover_client_id` -- which walks the SPA's chunks -- when that
id has actually stopped working.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable

from aiohttp import ClientError, ClientSession

from .const import API_BASE_PRIVATE, APP_BASE, BUNDLED_CLIENT_ID

_LOGGER = logging.getLogger(__name__)

#: `"trakt-api-key":`abc…`` and ``apiKey:`abc…`` as emitted by their minifier
_KEY_RE = re.compile(
    r"""["'`]?(?:trakt-api-key|apiKey)["'`]?\s*:\s*["'`]([0-9a-f]{64})["'`]""",
    re.IGNORECASE,
)
#: last-resort: any 64-hex literal in a chunk that clearly talks to the API
_LOOSE_KEY_RE = re.compile(r"[0-9a-f]{64}")
_CHUNK_RE = re.compile(r"/_app/immutable/(?:chunks|nodes|entry)/[A-Za-z0-9_.-]+\.js")

_REQUEST_TIMEOUT = 15
#: app.trakt.tv answers with a ~30 KB `Link:` preload header, well past
#: aiohttp's 8190-byte default field limit, so discovery needs its own session
#: rather than Home Assistant's shared one.
_MAX_HEADER = 128 * 1024
_CONCURRENCY = 10
#: stop crawling once we have pulled this much; the whole bundle is ~9 MB
_BYTE_BUDGET = 6 * 1024 * 1024
_TIME_BUDGET = 40


def _discovery_session() -> ClientSession:
    """A session tolerant of Trakt's oversized response headers."""
    return ClientSession(max_field_size=_MAX_HEADER, max_line_size=_MAX_HEADER)


async def _get_text(session: ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, timeout=_REQUEST_TIMEOUT) as response:
            if response.status != 200:
                return None
            return await response.text()
    except (ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return None


def _extract_key(text: str) -> str | None:
    if match := _KEY_RE.search(text):
        return match.group(1)
    # The minifier could change how the header object is written; if a chunk
    # mentions the API at all, take a 64-hex literal from it.
    if "trakt-api-key" in text or "apiz.trakt.tv" in text:
        if match := _LOOSE_KEY_RE.search(text):
            return match.group(0)
    return None


async def async_discover_client_id(app_base: str = APP_BASE) -> str | None:
    """Read the web app's client id out of its published JavaScript.

    Returns ``None`` if the bundle could not be reached or parsed, in which case
    the caller should stay on whatever id it already had.
    """
    async with _discovery_session() as session:
        return await _discover(session, app_base)


async def _discover(session: ClientSession, app_base: str) -> str | None:
    shell = await _get_text(session, f"{app_base}/")
    if shell is None:
        _LOGGER.debug("Could not fetch the Trakt web app shell")
        return None

    # The shell itself occasionally inlines it -- free to check.
    if key := _extract_key(shell):
        _LOGGER.debug("Found the Trakt client id in the app shell")
        return key

    chunks: list[str] = list(dict.fromkeys(_CHUNK_RE.findall(shell)))
    # The shell only names the entry bundles; those list every other chunk.
    for entry in list(chunks):
        if "/entry/" not in entry:
            continue
        if (text := await _get_text(session, f"{app_base}{entry}")) is None:
            continue
        if key := _extract_key(text):
            _LOGGER.debug("Found the Trakt client id in an entry bundle")
            return key
        chunks.extend(p for p in _CHUNK_RE.findall(text) if p not in chunks)

    targets = [p for p in chunks if "/entry/" not in p]
    if not targets:
        _LOGGER.debug("No Trakt web app chunks to search")
        return None

    _LOGGER.debug("Searching %d Trakt web app chunks for the client id", len(targets))
    return await _scan(session, app_base, targets)


async def _scan(
    session: ClientSession, app_base: str, targets: Iterable[str]
) -> str | None:
    """Fetch chunks concurrently, stopping at the first one carrying the id."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    for path in targets:
        queue.put_nowait(path)

    found: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    downloaded = 0
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal downloaded
        while not queue.empty() and not found.done():
            path = await queue.get()
            text = await _get_text(session, f"{app_base}{path}")
            if text is None:
                continue
            async with lock:
                downloaded += len(text)
                over_budget = downloaded > _BYTE_BUDGET
            if (key := _extract_key(text)) and not found.done():
                found.set_result(key)
                return
            if over_budget and not found.done():
                _LOGGER.debug("Gave up scanning after %d bytes", downloaded)
                found.set_result(None)
                return

    # Workers re-check `found` each iteration, so setting it drains them all
    # after at most one more fetch apiece.
    workers = [asyncio.create_task(worker()) for _ in range(_CONCURRENCY)]
    try:
        await asyncio.wait_for(
            asyncio.gather(*workers, return_exceptions=True), timeout=_TIME_BUDGET
        )
    except asyncio.TimeoutError:
        _LOGGER.debug("Timed out searching for the Trakt client id")
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    return found.result() if found.done() else None


async def async_validate_client_id(
    session: ClientSession, client_id: str, api_base: str = API_BASE_PRIVATE
) -> bool:
    """Check an id with one cheap unauthenticated call."""
    try:
        async with session.get(
            f"{api_base}/search/movie",
            params={"query": "the", "limit": "1"},
            headers={
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
            },
            timeout=_REQUEST_TIMEOUT,
        ) as response:
            return response.status == 200
    except (ClientError, asyncio.TimeoutError):
        # Can't tell a dead key from a dead network; assume the key is fine so
        # a blip doesn't trigger a pointless crawl.
        return True


async def async_resolve_client_id(
    session: ClientSession,
    current: str | None = None,
    *,
    force_discovery: bool = False,
) -> tuple[str, bool]:
    """Return a working client id and whether it came from a fresh discovery.

    Order of preference: the id we are already using (if it still works), then
    whatever the live bundle advertises, then the value baked into this repo.
    """
    if not force_discovery:
        candidate = current or BUNDLED_CLIENT_ID
        if await async_validate_client_id(session, candidate):
            return candidate, False
        _LOGGER.info("The Trakt client id in use was rejected; looking for a new one")

    if discovered := await async_discover_client_id():
        if await async_validate_client_id(session, discovered):
            if discovered != (current or BUNDLED_CLIENT_ID):
                _LOGGER.info("Picked up a new Trakt client id from the web app")
            return discovered, True
        _LOGGER.debug("The id found in the web app did not validate")

    fallback = current or BUNDLED_CLIENT_ID
    _LOGGER.warning(
        "Could not confirm a Trakt client id from the web app; keeping %s…",
        fallback[:8],
    )
    return fallback, False
