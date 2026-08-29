"""Identify an episode by matching the player's thumbnail to Trakt's stills.

The Apple TV app never reports a season/episode number, but it *does* expose an
``entity_picture`` — and for scripted shows that image is the very same frame
TMDB (and therefore Trakt) uses as the episode screenshot. A difference hash
lines them up decisively: in testing the correct episode scored a Hamming
distance of 3 out of 256 while every other episode sat above 100, so a plain
threshold separates them cleanly. Desaturation doesn't matter — the hash is
computed on luminance, so Trakt's black-and-white still still matches Apple's
colour one.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

#: dhash grid; 16 -> a 256-bit hash
_HASH_SIZE = 16
_BITS = _HASH_SIZE * _HASH_SIZE

#: A match must be at least this close (out of 256 bits)...
_MAX_DISTANCE = 24
#: ...and this much closer than the runner-up, so we only accept a clear winner.
_MIN_SEPARATION = 40

_TIMEOUT = 15


@dataclass(slots=True)
class ThumbnailMatch:
    season: int
    episode: int
    distance: int
    runner_up: int | None
    title: str | None


def _dhash(image: "Image.Image") -> int:
    """Difference hash of a PIL image (row-wise brightness gradients)."""
    small = image.convert("L").resize(
        (_HASH_SIZE + 1, _HASH_SIZE), _RESAMPLE
    )
    pixels = list(small.getdata())
    bits = 0
    for row in range(_HASH_SIZE):
        base = row * (_HASH_SIZE + 1)
        for col in range(_HASH_SIZE):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


try:  # Pillow ships with Home Assistant, but degrade gracefully if it's absent.
    from PIL import Image

    _RESAMPLE = Image.Resampling.LANCZOS
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    _RESAMPLE = 1
    _PIL_AVAILABLE = False


def available() -> bool:
    return _PIL_AVAILABLE


def hash_bytes(data: bytes) -> int | None:
    if not _PIL_AVAILABLE:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return _dhash(image)
    except Exception as err:  # noqa: BLE001 - any decode failure is non-fatal
        _LOGGER.debug("Could not hash image: %s", err)
        return None


async def async_fetch_and_hash(
    session: "ClientSession", url: str, headers: dict[str, str] | None = None
) -> int | None:
    if not _PIL_AVAILABLE:
        return None
    from aiohttp import ClientError

    try:
        async with session.get(url, headers=headers, timeout=_TIMEOUT) as response:
            if response.status != 200:
                return None
            data = await response.read()
    except (ClientError, asyncio.TimeoutError):
        return None
    return hash_bytes(data)


async def async_best_match(
    session: "ClientSession",
    target: int,
    candidates: list[dict[str, Any]],
) -> ThumbnailMatch | None:
    """Find which candidate episode still matches ``target`` (a dhash).

    ``candidates`` are dicts with ``season``, ``episode``, ``title`` and
    ``image`` (a screenshot URL). Returns a match only when one episode is both
    close enough and clearly ahead of the next best.
    """
    if not _PIL_AVAILABLE or not candidates:
        return None

    async def score(candidate: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        h = await async_fetch_and_hash(session, _https(candidate["image"]))
        if h is None:
            return None
        return hamming(target, h), candidate

    results = [r for r in await asyncio.gather(*(score(c) for c in candidates)) if r]
    if not results:
        return None

    results.sort(key=lambda r: r[0])
    best_distance, best = results[0]
    runner_up = results[1][0] if len(results) > 1 else None

    if best_distance > _MAX_DISTANCE:
        _LOGGER.debug(
            "Closest episode still was %d bits away (> %d); no thumbnail match",
            best_distance,
            _MAX_DISTANCE,
        )
        return None
    if runner_up is not None and runner_up - best_distance < _MIN_SEPARATION:
        _LOGGER.debug(
            "Thumbnail match ambiguous: best %d vs runner-up %d", best_distance, runner_up
        )
        return None

    return ThumbnailMatch(
        season=int(best["season"]),
        episode=int(best["episode"]),
        distance=best_distance,
        runner_up=runner_up,
        title=best.get("title"),
    )


def _https(url: str) -> str:
    if url.startswith("http"):
        return url
    return f"https://{url}"
