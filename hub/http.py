"""Shared async HTTP client: polite, retrying, capped per host.

Every connector goes through `Http`, so politeness is a property of the hub
rather than of whichever connector remembered it:

* one identifying User-Agent (operators can append a contact via HUB_CONTACT)
* a per-host concurrency cap, so syncing 300 Greenhouse boards never opens
  300 sockets to one vendor
* retries with exponential backoff on transport errors, 429 and 5xx, honouring
  Retry-After; 4xx other than 429 surfaces immediately so a 404 reads as
  "no such board" rather than as an outage
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

from hub import __version__

USER_AGENT = f"hiring-hub/{__version__} (+https://github.com/pete-builds/greenhouse-hiring-tracker-mcp)"
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0
PER_HOST_LIMIT = 4
_RETRY_STATUS = {429, 500, 502, 503, 504}

# httpx logs every request URL at INFO. No connector sends a secret today, but
# the aggregator connectors are where a keyed API would be added, and a key in
# a query string would then land in container logs on every call.
logging.getLogger("httpx").setLevel(logging.WARNING)


class Http:
    def __init__(
        self,
        *,
        timeout: float = REQUEST_TIMEOUT,
        per_host: int = PER_HOST_LIMIT,
        retries: int = MAX_RETRIES,
        backoff: float = RETRY_BACKOFF,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        contact = os.getenv("HUB_CONTACT")
        ua = f"{USER_AGENT} {contact}" if contact else USER_AGENT
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": ua, "Accept": "application/json, */*;q=0.5"},
            follow_redirects=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            transport=transport,
        )
        self._retries = retries
        self._backoff = backoff
        self._per_host = per_host
        self._sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_host)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._sems[host]:
                    resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in _RETRY_STATUS and attempt < self._retries:
                    await asyncio.sleep(_retry_after(resp) or self._backoff * 2**attempt)
                    continue
                resp.raise_for_status()
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * 2**attempt)
        assert last is not None
        raise last

    async def json(self, url: str, *, method: str = "GET", **kwargs):
        resp = await self.request(method, url, **kwargs)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            # A 200 that is not JSON is a captive portal, a CDN error page, or
            # a board that moved. Name it, rather than leaking a decoder error.
            raise BadPayload(f"{url} returned non-JSON ({resp.headers.get('content-type')})") from exc

    async def text(self, url: str, **kwargs) -> str:
        return (await self.request("GET", url, **kwargs)).text


class BadPayload(ValueError):
    """The upstream answered 200 with something that is not the expected shape."""


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return min(30.0, float(value)) if value else None
    except ValueError:
        return None
