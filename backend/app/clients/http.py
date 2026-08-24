"""Shared async HTTP client with retries and sane timeouts."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DEFAULT_HEADERS = {
    "User-Agent": "DFSEdge/0.1 (personal DFS research tool)",
    "Accept": "application/json",
}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class ApiError(RuntimeError):
    """Raised when an upstream API returns something we can't use."""

    def __init__(self, message: str, *, status: int | None = None, source: str = ""):
        super().__init__(message)
        self.status = status
        self.source = source


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source: str = "api",
    retries: int = 2,
) -> Any:
    """
    GET a URL and parse JSON, retrying on transient failures.

    Retries on timeouts, connection errors, 429 and 5xx. Does not retry
    on 4xx (a bad key or bad params won't fix itself).
    """
    client = get_client()
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise ApiError(
                f"Could not reach {source}: {exc}", source=source
            ) from exc

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(
                    f"{source} returned a non-JSON response", source=source
                ) from exc

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue

        detail = resp.text[:300]
        raise ApiError(
            f"{source} returned HTTP {resp.status_code}: {detail}",
            status=resp.status_code,
            source=source,
        )

    raise ApiError(f"{source} failed after retries: {last_error}", source=source)


async def get_bytes(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source: str = "api",
    retries: int = 2,
) -> bytes:
    """Same as get_json/get_text, but for binary payloads (e.g. a gzipped CSV)."""
    client = get_client()
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise ApiError(
                f"Could not reach {source}: {exc}", source=source
            ) from exc

        if resp.status_code == 200:
            return resp.content

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue

        detail = resp.text[:300]
        raise ApiError(
            f"{source} returned HTTP {resp.status_code}: {detail}",
            status=resp.status_code,
            source=source,
        )

    raise ApiError(f"{source} failed after retries: {last_error}", source=source)


async def get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source: str = "api",
    retries: int = 2,
) -> str:
    """Same as get_json, but for endpoints that return CSV/plain text."""
    client = get_client()
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise ApiError(
                f"Could not reach {source}: {exc}", source=source
            ) from exc

        if resp.status_code == 200:
            return resp.text

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue

        detail = resp.text[:300]
        raise ApiError(
            f"{source} returned HTTP {resp.status_code}: {detail}",
            status=resp.status_code,
            source=source,
        )

    raise ApiError(f"{source} failed after retries: {last_error}", source=source)
