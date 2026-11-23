"""Opt-in acceptance check for a REAL privately deployed SilverBullet MCP.

Default is read-only (MCP initialize/tools/list, optional read_page).
Writes are possible ONLY with both --write-test and --confirm-disposable-space.
All writes stay on the caller's chosen SilverBullet space and leave pages
behind. NEVER run write mode against your primary knowledge space.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastmcp import Client


EXPECTED = {
    "notes_search", "notes_read_page", "notes_capture",
    "notes_create_wiki_page", "notes_update_page",
    "notes_list_agent_feedback", "notes_resolve_agent_feedback",
    "notes_overview", "notes_search_status", "notes_search_ranked",
    "notes_related_pages",
}


def _result(call):
    if call.is_error:
        raise ValueError("MCP tool reported failure")
    data = call.structured_content
    if data is None:
        parts = [part.text for part in call.content if hasattr(part, "text")]
        if not parts:
            raise ValueError("Expected JSON tool result")
        data = json.loads(parts[0])
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    if not isinstance(data, dict):
        raise ValueError("Expected structured object from MCP tool")
    return data


def _endpoint(url: str) -> None:
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or
        parts.username or parts.password or parts.query or parts.fragment or
        not parts.path.rstrip("/").endswith("/mcp")):
        raise ValueError("Provide a private http(s) SilverBullet MCP URL ending in /mcp")


async def accept(url: str, *, read_page: str | None = None,
                 write_test: bool = False, bearer_token: str | None = None) -> dict:
    """Connect to real MCP transport; never print API tokens or page contents."""
    _endpoint(url)
    summary = {
        "catalog_checked": False,
        "read_checked": False,
        "write_checked": False,
        "etag_checked": False,
        "disposable_pages_created": 0,
        "production_registry_changed": False,
    }
    async with Client(url, timeout=20, auth=bearer_token) as client:
        names = {tool.name for tool in await client.list_tools()}
        if names != EXPECTED:
            raise ValueError("Live MCP catalog differs from the required seven tools")
        summary["catalog_checked"] = True

        if read_page:
            existing = _result(await client.call_tool(
                "notes_read_page", {"page": read_page}
            ))
            if existing.get("page") != read_page.removesuffix(".md"):
                raise ValueError("Read page response does not match requested page")
            if not isinstance(existing.get("content"), str):
                raise ValueError("Read page returned no content")
            summary["read_checked"] = True

        if write_test:
            nonce = uuid4().hex[:16]
            page = "Wiki/MCP-Acceptance-" + nonce
            body = "# MCP acceptance " + nonce + "\n\n"
            body += "Disposable silverbullet verification " + nonce + ".\n\n"
            body += "<!-- @agent Check MCP acceptance -->\n"
            created = _result(await client.call_tool(
                "notes_create_wiki_page", {"page": page, "content": body}
            ))
            if created.get("page") != page:
                raise ValueError("Create page returned the wrong page")
            summary["disposable_pages_created"] += 1
            read = _result(await client.call_tool(
                "notes_read_page", {"page": page}
            ))
            if read.get("content") != body or not read.get("etag"):
                raise ValueError("Created page was not readable with an ETag")
            found = _result(await client.call_tool(
                "notes_search", {"query": nonce}
            ))
            if page not in [row.get("page") for row in found.get("results", [])]:
                raise ValueError("New page was not indexed by filesystem search")
            feedback = _result(await client.call_tool(
                "notes_list_agent_feedback", {"limit": 100}
            ))
            matching = [item for item in feedback.get("results", [])
                        if item.get("page") == page]
            if len(matching) != 1 or not matching[0].get("id"):
                raise ValueError("New page feedback was not listed")
            resolved = _result(await client.call_tool(
                "notes_resolve_agent_feedback",
                {"page": page, "feedback_id": matching[0]["id"], "etag": read["etag"]},
            ))
            if resolved.get("page") != page or not resolved.get("etag"):
                raise ValueError("Feedback resolve returned no new ETag")

            new_body = "# MCP acceptance " + nonce + "\n\nUpdated via MCP.\n"
            updated = _result(await client.call_tool(
                "notes_update_page",
                {"page": page, "content": new_body, "etag": resolved["etag"]},
            ))
            if updated.get("page") != page:
                raise ValueError("Update returned the wrong page")
            stale = await client.call_tool(
                "notes_update_page",
                {"page": page, "content": "# STALE WRITE\n", "etag": read["etag"]},
                raise_on_error=False,
            )
            if not stale.is_error:
                raise ValueError("Stale ETag write was incorrectly accepted")
            final = _result(await client.call_tool(
                "notes_read_page", {"page": page}
            ))
            if final.get("content") != new_body:
                raise ValueError("Stale write changed the page unexpectedly")
            summary["etag_checked"] = True

            note = _result(await client.call_tool(
                "notes_capture",
                {"title": "MCP acceptance " + nonce,
                 "content": "Disposable test note " + nonce},
            ))
            if not note.get("page", "").startswith("Inbox/"):
                raise ValueError("create_note did not return an Inbox page")
            summary["disposable_pages_created"] += 1
            summary["read_checked"] = True
            summary["write_checked"] = True

    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True,
                        help="Private MCP endpoint, e.g. http://127.0.0.1:8773/mcp")
    parser.add_argument("--read-page",
                        help="Existing page to read in default read-only mode")
    parser.add_argument("--mcp-token-file", type=Path,
                        help="Private read-only MCP service bearer token file")
    parser.add_argument("--write-test", action="store_true",
                        help="Create two permanent disposable pages and exercise write/ETag")
    parser.add_argument("--confirm-disposable-space", action="store_true",
                        help="I confirm this MCP is connected ONLY to a disposable SilverBullet space")
    args = parser.parse_args(argv)
    if args.write_test != args.confirm_disposable_space:
        parser.error("--write-test requires --confirm-disposable-space, and vice versa")
    try:
        result = asyncio.run(accept(
            args.url, read_page=args.read_page, write_test=args.write_test,
            bearer_token=args.mcp_token_file.read_text().strip()
            if args.mcp_token_file else None,
        ))
    except Exception as error:
        parser.exit(2, f"Acceptance check failed ({type(error).__name__})\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
