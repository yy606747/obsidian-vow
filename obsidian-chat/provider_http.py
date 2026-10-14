"""Reuse provider connections within the application's event-loop lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import weakref

import httpx


class _ClientPool:
    def __init__(self):
        self.clients: dict[tuple, httpx.AsyncClient] = {}
        self.lock = asyncio.Lock()
        self.stack = AsyncExitStack()

    async def get(self, options: dict) -> httpx.AsyncClient:
        key = tuple(sorted(options.items()))
        async with self.lock:
            client = self.clients.get(key)
            if client is None:
                client = await self.stack.enter_async_context(httpx.AsyncClient(
                    **options,
                    limits=httpx.Limits(
                        max_connections=100, max_keepalive_connections=20,
                        keepalive_expiry=60.0,
                    ),
                ))
                self.clients[key] = client
            return client


_pools: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@asynccontextmanager
async def provider_http_lifespan():
    """Close shared clients after all application work has been drained."""
    loop = asyncio.get_running_loop()
    if loop in _pools:
        yield
        return
    pool = _ClientPool()
    _pools[loop] = pool
    try:
        yield
    finally:
        _pools.pop(loop, None)
        await pool.stack.aclose()


@asynccontextmanager
async def provider_client(*, proxy: str | None = None, trust_env: bool = True):
    # Deadlines vary per request (notably selector retries). Keep them on
    # post()/stream(), otherwise each remaining-time value creates a new pool.
    options = {"proxy": proxy, "trust_env": trust_env}
    pool = _pools.get(asyncio.get_running_loop())
    if pool is None:
        # One-shot scripts and separate worker loops own their own connections.
        async with httpx.AsyncClient(**options) as client:
            yield client
    else:
        yield await pool.get(options)
