"""Thin Tavily Search adapter."""

from __future__ import annotations

import asyncio
import os

import httpx

from config import SETTINGS


TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SEC = 15.0


def tavily_api_key() -> str:
    return str(
        os.environ.get("AION_TAVILY_API_KEY")
        or SETTINGS.get("tavily_api_key")
        or ""
    ).strip()


class TavilyProviderError(RuntimeError):
    pass


class TavilySearchProvider:
    async def search(self, query: str) -> dict:
        key = tavily_api_key()
        if not key:
            raise TavilyProviderError("missing_api_key")
        payload = {
            "query": str(query).strip(),
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": False,
            "include_raw_content": False,
            "auto_parameters": False,
        }
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT_SEC, trust_env=False) as client:
                    response = await client.post(
                        TAVILY_URL,
                        headers={"Authorization": f"Bearer {key}"},
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0)
                    continue
                raise TavilyProviderError(f"network:{type(exc).__name__}") from exc
            if response.status_code != 200:
                raise TavilyProviderError(f"http_{response.status_code}")
            data = response.json()
            return {
                "request_id": str(data.get("request_id") or ""),
                "results": [
                    {
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                        "content": str(item.get("content") or "").strip(),
                        "score": item.get("score"),
                        "published_date": str(item.get("published_date") or "").strip(),
                    }
                    for item in list(data.get("results") or [])[:5]
                    if isinstance(item, dict)
                ],
            }
        raise TavilyProviderError("network_failed")


__all__ = ["TavilyProviderError", "TavilySearchProvider", "tavily_api_key"]
