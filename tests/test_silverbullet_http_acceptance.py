"""End-to-end SilverBullet MCP transport acceptance, using disposable data.

Exercises REAL HTTP Streamable MCP via loopback and all seven knowledge tools.
The SilverBullet API is emulated locally and shares a temporary Markdown
space. No existing SilverBullet space, token, Gateway or production port is used.
This cannot establish compatibility with a particular deployed SilverBullet
version; a separate disposable real-server acceptance is still required.
"""
from __future__ import annotations

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from fastmcp import Client
import uvicorn

from silverbullet_mcp.server import mcp
from scripts.check_silverbullet_http import accept, main as acceptance_cli


EXPECTED = {
    "notes_search", "notes_read_page", "notes_capture",
    "notes_create_wiki_page", "notes_update_page",
    "notes_list_agent_feedback", "notes_resolve_agent_feedback",
}
TOKEN = "disposable-silverbullet-token"


def fake_api(space: Path):
    """Tiny fake for /.fs/*; this is NOT a replacement SilverBullet server."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _target(self):
            url = unquote(urlsplit(self.path).path)
            if not url.startswith("/.fs/"):
                return None
            relative = url.removeprefix("/.fs/")
            parts = relative.split("/")
            if (not all(parts) or
                any(part in {".", ".."} for part in parts) or
                parts[0] not in {"Wiki", "Inbox"} or
                not relative.endswith(".md")):
                return None
            return space.joinpath(*parts)

        @staticmethod
        def _etag(data: bytes) -> str:
            return '"sha256:' + hashlib.sha256(data).hexdigest() + '"'

        def _reply(self, status, body=b"", etag=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _dispatch(self, method):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                self._reply(401)
                return
            if self.headers.get("X-Sync-Mode") != "true":
                self._reply(400)
                return
            path = self._target()
            if path is None:
                self._reply(404)
                return
            if method == "GET":
                if not path.is_file():
                    self._reply(404)
                    return
                data = path.read_bytes()
                self._reply(200, data, self._etag(data))
                return
            exists = path.is_file()
            if self.headers.get("If-None-Match") == "*" and exists:
                self._reply(412)
                return
            if self.headers.get("If-Match") is not None:
                if not exists or self.headers["If-Match"] != self._etag(path.read_bytes()):
                    self._reply(412)
                    return
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 500_000:
                self._reply(400)
                return
            data = self.rfile.read(size)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._reply(200 if exists else 201, etag=self._etag(data))

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

    return Handler


def _data(result):
    if result.is_error:
        raise AssertionError("MCP tool returned error: " + str(result.content))
    data = result.structured_content
    if data is None:
        text_parts = [part.text for part in result.content if hasattr(part, "text")]
        if not text_parts:
            raise AssertionError("Tool returned neither JSON nor structured content")
        data = json.loads(text_parts[0])
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    if not isinstance(data, dict):
        raise AssertionError("Expected structured dictionary tool output")
    return data


class SilverBulletHTTPAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.space = root / "space"
        self.space.mkdir()
        self.token = root / "silverbullet-token"
        self.token.write_text(TOKEN, encoding="utf-8")
        os.chmod(self.token, 0o600)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), fake_api(self.space))
        self.api_thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.api_thread.start()
        def shutdown_api():
            self.api.shutdown()
            self.api.server_close()
            self.api_thread.join(timeout=5)

        self.addCleanup(shutdown_api)
        self.environ = patch.dict(os.environ, {
            "KNOWLEDGE_SPACE_DIR": str(self.space),
            "KNOWLEDGE_TOKEN_FILE": str(self.token),
            "KNOWLEDGE_BASE_URL": "http://127.0.0.1:" + str(self.api.server_port),
        })
        self.environ.start()
        self.addCleanup(self.environ.stop)

    async def test_all_seven_tools_over_actual_mcp_http_with_etag_and_feedback(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(
            mcp.http_app(path="/mcp", stateless_http=True),
            log_level="error", lifespan="on",
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.02)
            self.assertTrue(server.started, "Loopback MCP server did not start")
            url = "http://127.0.0.1:" + str(listener.getsockname()[1]) + "/mcp"
            async with Client(url, timeout=10) as client:
                catalog = await client.list_tools()
                self.assertEqual(EXPECTED, {tool.name for tool in catalog})

                body = "# Rehearsal\n\nBoundary verification sentence.\n\n"
                body += "<!-- @agent Verify this sentence. -->\n"
                created = _data(await client.call_tool(
                    "notes_create_wiki_page",
                    {"page": "Wiki/Rehearsal", "content": body},
                ))
                self.assertEqual("Wiki/Rehearsal", created["page"])
                self.assertTrue((self.space / "Wiki/Rehearsal.md").is_file())

                read = _data(await client.call_tool(
                    "notes_read_page", {"page": "Wiki/Rehearsal"}
                ))
                self.assertEqual(body, read["content"])
                self.assertTrue(read["etag"].startswith('"sha256:'))

                search = _data(await client.call_tool(
                    "notes_search", {"query": "Boundary verification"}
                ))
                self.assertIn("Wiki/Rehearsal",
                              [item["page"] for item in search["results"]])

                feedback = _data(await client.call_tool(
                    "notes_list_agent_feedback", {"limit": 10}
                ))
                self.assertEqual(1, feedback["total"])
                feedback_id = feedback["results"][0]["id"]

                resolved = _data(await client.call_tool(
                    "notes_resolve_agent_feedback",
                    {"page": "Wiki/Rehearsal", "feedback_id": feedback_id,
                     "etag": read["etag"]},
                ))
                self.assertEqual(feedback_id, resolved["resolved"])
                self.assertNotIn("@agent", (self.space / "Wiki/Rehearsal.md").read_text())

                updated_text = "# Rehearsal\n\nUpdated page through MCP.\n"
                updated = _data(await client.call_tool(
                    "notes_update_page",
                    {"page": "Wiki/Rehearsal", "content": updated_text,
                     "etag": resolved["etag"]},
                ))
                self.assertEqual("Wiki/Rehearsal", updated["page"])
                self.assertEqual(updated_text, (self.space / "Wiki/Rehearsal.md").read_text())

                stale = await client.call_tool(
                    "notes_update_page",
                    {"page": "Wiki/Rehearsal", "content": "# stale\n",
                     "etag": read["etag"]}, raise_on_error=False,
                )
                self.assertTrue(stale.is_error, "Stale ETag must fail")
                self.assertEqual(updated_text, (self.space / "Wiki/Rehearsal.md").read_text())

                note = _data(await client.call_tool(
                    "notes_capture",
                    {"title": "Smoke test", "content": "Temporary test only"},
                ))
                self.assertTrue(note["page"].startswith("Inbox/"))
                self.assertTrue((self.space / (note["page"] + ".md")).is_file())

                invalid_path = await client.call_tool(
                    "notes_create_wiki_page",
                    {"page": "Wiki/../CONFIG", "content": "# Nope"},
                    raise_on_error=False,
                )
                self.assertTrue(invalid_path.is_error, "Path traversal must fail")
                self.assertFalse((self.space / "CONFIG.md").exists())

                # Exercise the actual operator acceptance command through
                # the same real MCP transport, still on disposable resources.
                default_check = await accept(url, read_page="Wiki/Rehearsal")
                self.assertTrue(default_check["catalog_checked"])
                self.assertTrue(default_check["read_checked"])
                self.assertFalse(default_check["write_checked"])
                full_check = await accept(url, write_test=True)
                self.assertTrue(full_check["write_checked"])
                self.assertTrue(full_check["etag_checked"])
                self.assertEqual(2, full_check["disposable_pages_created"])

                self.token.write_text("revoked-test-token", encoding="utf-8")
                unauthorized = await client.call_tool(
                    "notes_read_page", {"page": "Wiki/Rehearsal"},
                    raise_on_error=False,
                )
                self.assertTrue(unauthorized.is_error,
                                "API request must fail when token is invalid")
        finally:
            server.should_exit = True
            await task
            listener.close()


    def test_real_endpoint_write_mode_requires_disposable_confirmation(self):
        with self.assertRaises(SystemExit) as context:
            acceptance_cli([
                "--url", "http://127.0.0.1:8773/mcp", "--write-test",
            ])
        self.assertEqual(2, context.exception.code)

    def test_rejects_credential_bearing_or_non_mcp_endpoint(self):
        from scripts.check_silverbullet_http import _endpoint
        for url in (
            "http://name:secret@localhost:8773/mcp",
            "http://localhost:8773/not-mcp",
            "https://localhost:8773/mcp?token=secret",
            "file:///tmp/mcp",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _endpoint(url)


if __name__ == "__main__":
    unittest.main()
