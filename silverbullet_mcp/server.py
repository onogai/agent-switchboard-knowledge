"""Standalone SilverBullet MCP. Does not import the Switchboard Gateway.

Deploy it on a private network, then register it as an HTTP source in
Switchboard Manager. Do not publish this unauthenticated endpoint to the
internet; Switchboard handles user OAuth and tool grants.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
import stat

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from mcp.types import ToolAnnotations

from .knowledge import KnowledgeClient, KnowledgeError, list_feedback, search_pages, list_pages, backlinks
from .documents import query_pages
from .v3_api import overview as v3_overview, search_status as v3_search_status, find as v3_find, related as v3_related


class PrivateBearerVerifier(TokenVerifier):
    """Internal service-to-service opaque bearer verifier (no OAuth server).

    The expected token is read from a private, read-only file on startup.
    Compare in constant time; do not print/store the token in Registry logs.
    """

    def __init__(self, token_file: Path):
        super().__init__()
        metadata = token_file.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("MCP token file must be a private regular file")
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) < 48 or not token.isascii() or not token.isprintable():
            raise ValueError("MCP bearer token is missing or too short")
        self._expected = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._expected):
            return None
        return AccessToken(
            token=token,
            client_id="switchboard-core",
            subject="switchboard-core",
            scopes=[],
        )


def _authentication() -> TokenVerifier | None:
    """Allow anonymous MCP only when bound to *container* loopback.

    Network-reachable MCP must have a dedicated, private bearer token file.
    The SilverBullet API token is not accepted here.
    """
    bind = os.environ.get("SILVERBULLET_MCP_BIND_HOST", "127.0.0.1")
    filename = os.environ.get("SILVERBULLET_MCP_TOKEN_FILE")
    if filename:
        return PrivateBearerVerifier(Path(filename))
    if bind != "127.0.0.1":
        raise ValueError("Network-reachable MCP requires SILVERBULLET_MCP_TOKEN_FILE")
    return None


mcp = FastMCP("Switchboard SilverBullet", auth=_authentication())


async def notes_search(query: str, limit: int = 10, include_excluded: bool = False) -> dict:
    """Use to locate notes by keywords; returns page paths and excerpts, not full Markdown. Query must be 2–200 characters, limit 1–30. Read a matching page using notes_read_page. Report folders are excluded unless include_excluded=True."""
    client = KnowledgeClient.from_environment()
    return await asyncio.to_thread(search_pages, client.root, query, limit, include_excluded)


async def notes_list_pages(prefix: str = "", limit: int = 50, offset: int = 0) -> dict:
    """Browse actual Markdown page paths, optionally limited to a folder subtree such as Wiki/Automobiles. Returns a bounded page list and pagination details."""
    client = KnowledgeClient.from_environment()
    return await asyncio.to_thread(list_pages, client.root, prefix, limit, offset)


async def notes_query_pages(prefix: str, date_from: str, date_to: str,
                            date_field: str = "date", metadata_equals: dict[str, str] | None = None,
                            fields: list[str] | None = None, include_content: bool = False,
                            limit: int = 30, offset: int = 0, max_bytes: int = 60000) -> dict:
    """Retrieve dated Markdown documents in a folder subtree using YAML frontmatter, not file modification time or the search index. Inclusive YYYY-MM-DD dates; date_field defaults to date. metadata_equals matches scalar values or membership in scalar lists (e.g. tags). fields selects metadata keys (None=all, []=none); include_content includes Markdown bodies. Newest date first, then page path. Follow next_offset when truncated; max_bytes bounds the JSON response. Includes search-excluded folders."""
    client = KnowledgeClient.from_environment()
    return await asyncio.to_thread(query_pages, client.root, prefix, date_from, date_to,
                                   date_field, metadata_equals, fields, include_content,
                                   limit, offset, max_bytes)


async def notes_backlinks(page: str, limit: int = 30) -> dict:
    """Find explicit SilverBullet wiki links pointing at a specific page. Returns source pages, lines and excerpts; this does not infer semantic relationships."""
    client = KnowledgeClient.from_environment()
    return await asyncio.to_thread(backlinks, client.root, page, limit)


async def notes_overview() -> dict:
    """Discover available folders, top-level pages, skills and tags via SilverBullet 3.0 /.help. Prefer this before searching when unfamiliar with the knowledge space."""
    return await asyncio.to_thread(v3_overview)


async def notes_search_status() -> dict:
    """Check which SilverBullet 3.0 search modes are ready. Semantic mode is optional and can be disabled or still indexing."""
    return await asyncio.to_thread(v3_search_status)


async def notes_search_ranked(query: str, mode: str = "words", limit: int = 10,
                              await_index: bool = False, include_excluded: bool = False) -> dict:
    """Ranked server-index search. 'words' for exact topics, 'phrase' for a quotation, 'natural' for semantic intent (only when enabled). Uses the available index by default; recent writes may be missing. Set await_index=True to request bounded catch-up, not guaranteed embedding freshness. Inspect index_status (if returned), partial and notice; read source pages. Configured report folders are excluded unless include_excluded=True."""
    return await asyncio.to_thread(v3_find, query, mode, limit, await_index, include_excluded)


async def notes_related_pages(page: str, limit: int = 5, include_excluded: bool = False) -> dict:
    """Find pages semantically related to a known page (requires enabled, ready semantic search). Read matching pages before using them as facts. Configured report folders are excluded unless include_excluded=True."""
    return await asyncio.to_thread(v3_related, page, limit, include_excluded)


async def notes_read_page(page: str) -> dict:
    """Read the complete Markdown of a SilverBullet page and its current ETag. Use after notes_search or before any update or feedback resolution. Pass page path without optional .md."""
    return await asyncio.to_thread(KnowledgeClient.from_environment().read_page, page)


def _require_write_mode() -> None:
    # Default deny until an explicitly trusted deployment opts into writes.
    # A staging adapter advertises the stable seven-tool catalog but cannot
    # mutate existing knowledge even if called directly over its private MCP.
    if os.environ.get("SILVERBULLET_MCP_WRITE_ENABLED") != "true":
        raise KnowledgeError("Knowledge writes are disabled for this MCP deployment.")


async def notes_capture(title: str, content: str) -> dict:
    """Capture a new note or conversation result as a uniquely named page under Inbox/. Supply a title and Markdown content; never overwrites another note. For a named Wiki/ page use notes_create_wiki_page."""
    _require_write_mode()
    return await asyncio.to_thread(KnowledgeClient.from_environment().create_note, title, content)


async def notes_create_wiki_page(page: str, content: str, idempotent: bool = False) -> dict:
    """Create a named Wiki/ page with complete Markdown, refusing to overwrite existing pages. Use notes_capture to store unstructured quick notes in Inbox/. For recurring documents use a deterministic path and idempotent=True: an identical existing document succeeds without rewriting; different content conflicts."""
    _require_write_mode()
    return await asyncio.to_thread(KnowledgeClient.from_environment().create_page, page, content, idempotent)


async def notes_update_page(page: str, content: str, etag: str) -> dict:
    """Replace the ENTIRE content of an existing Wiki/ or Inbox/ page using the ETag from notes_read_page. Fails on concurrent changes; re-read and reconcile. This does not append or patch."""
    _require_write_mode()
    return await asyncio.to_thread(KnowledgeClient.from_environment().update_page,
                                   page, content, etag)


async def notes_edit_page(page: str, old_text: str, new_text: str, etag: str) -> dict:
    """Replace exactly one occurrence of old_text in an existing Wiki/ or Inbox/ page. Requires the current ETag; refuses ambiguous matches and concurrent edits."""
    _require_write_mode()
    return await asyncio.to_thread(KnowledgeClient.from_environment().edit_page,
                                   page, old_text, new_text, etag)


async def notes_move_page(source: str, destination: str, etag: str) -> dict:
    """Move an existing Inbox/ or Wiki/ Markdown page to a free path using conditional writes. Requires a current ETag. Backlinks are not rewritten; conflicts abort safely."""
    _require_write_mode()
    return await asyncio.to_thread(KnowledgeClient.from_environment().move_page,
                                   source, destination, etag)


async def notes_delete_page(page: str, etag: str, confirm: bool = False) -> dict:
    """Permanently delete one Inbox/ or Wiki/ Markdown page. Requires a current ETag and explicit confirm=true; rejects stale revisions and protected paths."""
    _require_write_mode()
    return await asyncio.to_thread(KnowledgeClient.from_environment().delete_page,
                                   page, etag, confirm)


async def notes_list_agent_feedback(limit: int = 20) -> dict:
    """Find pending @agent HTML comments in Markdown notes; return feedback ID, page, text and preceding context. Read the current page before calling notes_resolve_agent_feedback."""
    client = KnowledgeClient.from_environment()
    return await asyncio.to_thread(list_feedback, client.root, limit)


async def notes_resolve_agent_feedback(page: str, feedback_id: str, etag: str) -> dict:
    """Remove exactly one completed @agent feedback comment. Requires page, feedback ID from notes_list_agent_feedback, and current ETag from notes_read_page. Fails if page changed or ID ambiguous."""
    _require_write_mode()
    client = KnowledgeClient.from_environment()
    return await asyncio.to_thread(client.resolve_feedback, page, feedback_id, etag)


_READ_ONLY = {"notes_query_pages", "notes_search", "notes_read_page", "notes_list_agent_feedback", "notes_overview", "notes_search_status", "notes_search_ranked", "notes_related_pages", "notes_list_pages", "notes_backlinks"}
_DESTRUCTIVE = {"notes_update_page", "notes_edit_page", "notes_move_page", "notes_delete_page", "notes_resolve_agent_feedback"}

for _tool in (
    notes_search, notes_read_page, notes_overview, notes_search_status,
    notes_search_ranked, notes_related_pages, notes_list_pages, notes_backlinks, notes_query_pages,
    notes_capture, notes_create_wiki_page, notes_update_page, notes_edit_page,
    notes_move_page, notes_delete_page,
    notes_list_agent_feedback, notes_resolve_agent_feedback,
):
    mcp.tool(_tool, annotations=ToolAnnotations(
        read_only_hint=_tool.__name__ in _READ_ONLY,
        destructive_hint=_tool.__name__ in _DESTRUCTIVE,
        open_world_hint=False,
    ))


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=os.environ.get("SILVERBULLET_MCP_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("SILVERBULLET_MCP_PORT", "8773")),
    )
