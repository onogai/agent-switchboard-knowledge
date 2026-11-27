"""Read-only, bounded SilverBullet 3.0 HTTP API integrations.

Never expose /.shell, /.proxy, /.runtime or upstream OAuth through this adapter.
The upstream API token is confined to the private container network.
"""
from __future__ import annotations

import httpx
import time

from .knowledge import (KnowledgeClient, KnowledgeError, page_path,
                        search_exclude_prefixes, excluded_page_names, is_excluded)

MAX_JSON_BYTES = 200_000
MODES = {"words", "phrase", "natural"}


def _api(method: str, endpoint: str, *, payload: dict | None = None,
         await_index: bool = False) -> dict:
    client = KnowledgeClient.from_environment()
    headers = {
        "Authorization": f"Bearer {client._token()}",
        "Accept": "application/json",
        "X-Sync-Mode": "true",
    }
    if await_index:
        headers["X-Await-Index"] = "1"
    try:
        with httpx.Client(base_url=client.base_url, timeout=20, follow_redirects=False,
                          trust_env=False) as http:
            for attempt in range(3):
                response = http.request(method, endpoint, headers=headers, json=payload)
                # 429 means SearchError::Busy in SilverBullet 3, not a user quota.
                # Only bounded, read-only searches may be retried.
                if (response.status_code != 429 or
                        endpoint not in {"/.search/query", "/.search/related"} or
                        attempt == 2):
                    break
                retry_after = response.headers.get("retry-after", "1")
                delay = int(retry_after) if retry_after.isdigit() else 1
                time.sleep(min(2, max(1, delay)))
    except httpx.RequestError as exc:
        raise KnowledgeError("SilverBullet 3.0 API is unreachable.") from exc
    if response.status_code in {401, 403}:
        raise KnowledgeError("SilverBullet API token lacks access to this space.")
    if response.status_code == 404:
        raise KnowledgeError("SilverBullet 3.0 API endpoint is unavailable. Check version and space mode.")
    if response.status_code == 429:
        raise KnowledgeError("SilverBullet search is busy after bounded retries; try again shortly.")
    if response.status_code >= 300:
        raise KnowledgeError(f"SilverBullet 3.0 API returned HTTP {response.status_code}.")
    if len(response.content) > MAX_JSON_BYTES:
        raise KnowledgeError("SilverBullet API response exceeds the 200 KB tool limit.")
    try:
        parsed = response.json()
    except ValueError as exc:
        raise KnowledgeError("SilverBullet API response is not JSON.") from exc
    if not isinstance(parsed, dict):
        raise KnowledgeError("SilverBullet API response is not a JSON object.")
    if await_index and "x-index-status" in response.headers:
        parsed["index_status"] = response.headers["x-index-status"]
    return parsed


def overview() -> dict:
    """Bounded discovery of top-level pages, folders, tags and available skills."""
    result = _api("GET", "/.help")
    if result.get("version") != 1 or not isinstance(result.get("outline"), dict):
        raise KnowledgeError("Unexpected SilverBullet 3.0 help schema.")
    return result


def search_status() -> dict:
    """Report active search backend and whether semantic search is enabled and ready."""
    return _api("GET", "/.search/status")


def _search_payload(payload: dict, include_excluded: bool) -> tuple[dict, tuple[str, ...]]:
    prefixes = () if include_excluded else search_exclude_prefixes()
    if prefixes:
        names = excluded_page_names(KnowledgeClient.from_environment().root, prefixes)
        if names:
            payload["exclude"] = names
    return payload, prefixes


def _search_result(result: dict, prefixes: tuple[str, ...]) -> dict:
    if not isinstance(result.get("items"), list):
        raise KnowledgeError("SilverBullet search returned an unexpected result.")
    # The local directory snapshot may race an index update.
    items = [item for item in result["items"] if not is_excluded(item["page"], prefixes)]
    if len(items) != len(result["items"]):
        result = {**result, "items": items, "partial": True,
                  "notice": ((result.get("notice") or "") +
                             " Excluded pages removed from a stale search snapshot.").strip()}
    return result


def find(query: str, mode: str = "words", limit: int = 10,
         await_index: bool = False, include_excluded: bool = False) -> dict:
    """Server-side ranked text/semantic search. Never silently claim semantic readiness."""
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 200:
        raise KnowledgeError("Search query must be 2–200 characters.")
    if mode not in MODES:
        raise KnowledgeError("Mode must be words, phrase, or natural.")
    if not isinstance(limit, int) or not 1 <= limit <= 30:
        raise KnowledgeError("Limit must be 1–30.")
    if not isinstance(await_index, bool):
        raise KnowledgeError("await_index must be a boolean.")
    payload, prefixes = _search_payload(
        {"query": query.strip(), "mode": mode, "scope": "pages", "limit": limit},
        include_excluded)
    return _search_result(_api("POST", "/.search/query", await_index=await_index,
                               payload=payload), prefixes)


def related(page: str, limit: int = 5, include_excluded: bool = False) -> dict:
    """Use the semantic index to find pages related to the named page."""
    if not isinstance(limit, int) or not 1 <= limit <= 15:
        raise KnowledgeError("Limit must be 1–15.")
    page = page_path(page).removesuffix(".md")
    payload, prefixes = _search_payload(
        {"page": page, "limit": limit, "scope": "pages"}, include_excluded)
    return _search_result(_api("POST", "/.search/related", payload=payload), prefixes)
