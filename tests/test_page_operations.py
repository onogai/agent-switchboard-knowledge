"""Focused offline CRUD tests for bounded, revision-safe Knowledge operations."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import httpx

from silverbullet_mcp.knowledge import (
    KnowledgeClient, KnowledgeError, backlinks, list_pages,
)


def revision(data: bytes) -> str:
    return '"sha256:' + hashlib.sha256(data).hexdigest() + '"'


class FakeFiles:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.fail_source_delete = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-token"
        assert request.url.path.startswith("/.fs/")
        path = unquote(request.url.path.removeprefix("/.fs/"))
        existing = self.files.get(path)
        if request.method == "GET":
            if existing is None:
                return httpx.Response(404)
            return httpx.Response(200, content=existing,
                                  headers={"ETag": revision(existing)})
        if request.method == "PUT":
            if request.headers.get("if-none-match") == "*" and existing is not None:
                return httpx.Response(412)
            if "if-match" in request.headers and (
                existing is None or request.headers["if-match"] != revision(existing)
            ):
                return httpx.Response(412)
            self.files[path] = request.content
            return httpx.Response(200, headers={"ETag": revision(request.content)})
        if request.method == "DELETE":
            if self.fail_source_delete and path == "Wiki/Source.md":
                return httpx.Response(412)
            if existing is None:
                return httpx.Response(404)
            if request.headers.get("if-match") != revision(existing):
                return httpx.Response(412)
            del self.files[path]
            return httpx.Response(200)
        raise AssertionError(request.method)


class PageOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.space = Path(self.tmp.name) / "space"
        self.space.mkdir()
        self.token = Path(self.tmp.name) / "token"
        self.token.write_text("test-token")
        self.client = KnowledgeClient("http://silverbullet:3000", self.token, self.space)
        self.backend = FakeFiles()
        original_client = httpx.Client
        transport = httpx.MockTransport(self.backend.handle)

        def factory(*args, **kwargs):
            return original_client(*args, transport=transport, **kwargs)

        patched = patch("silverbullet_mcp.knowledge.httpx.Client", side_effect=factory)
        patched.start()
        self.addCleanup(patched.stop)

    def test_exact_edit_and_revision_conflicts(self):
        source = b"# Source\n\nKeep this.\n\nChange this once.\n"
        self.backend.files["Wiki/Source.md"] = source
        original_etag = revision(source)
        result = self.client.edit_page("Wiki/Source", "Change this once.",
                                       "Updated sentence.", original_etag)
        self.assertEqual("Wiki/Source", result["page"])
        self.assertEqual(b"# Source\n\nKeep this.\n\nUpdated sentence.\n",
                         self.backend.files["Wiki/Source.md"])
        with self.assertRaisesRegex(KnowledgeError, "changed since it was read"):
            self.client.edit_page("Wiki/Source", "Keep this.", "Wrong.", original_etag)
        with self.assertRaisesRegex(KnowledgeError, "match exactly once"):
            self.client.edit_page("Wiki/Source", "not present", "Wrong.", result["etag"])

    def test_move_conflict_rolls_back_copy(self):
        source = b"# Source\n\nMove me.\n"
        self.backend.files["Wiki/Source.md"] = source
        self.backend.fail_source_delete = True
        with self.assertRaisesRegex(KnowledgeError, "Move aborted"):
            self.client.move_page("Wiki/Source", "Wiki/Destination", revision(source))
        self.assertEqual(source, self.backend.files["Wiki/Source.md"])
        self.assertNotIn("Wiki/Destination.md", self.backend.files)

    def test_move_and_confirmed_delete_require_current_revision(self):
        source = b"# Source\n\nMove me.\n"
        self.backend.files["Wiki/Source.md"] = source
        moved = self.client.move_page("Wiki/Source", "Wiki/Destination", revision(source))
        self.assertFalse(moved["links_updated"])
        self.assertNotIn("Wiki/Source.md", self.backend.files)
        self.assertEqual(source, self.backend.files["Wiki/Destination.md"])
        with self.assertRaisesRegex(KnowledgeError, "confirm=true"):
            self.client.delete_page("Wiki/Destination", moved["etag"])
        with self.assertRaisesRegex(KnowledgeError, "changed since"):
            self.client.delete_page("Wiki/Destination", '"sha256:' + "0" * 64 + '"', True)
        self.assertTrue(self.client.delete_page("Wiki/Destination", moved["etag"], True)["deleted"])
        self.assertNotIn("Wiki/Destination.md", self.backend.files)

    def test_list_and_backlinks_only_read_visible_markdown(self):
        (self.space / "Wiki").mkdir()
        (self.space / "Wiki" / "Target.md").write_text("# Target\n")
        (self.space / "Wiki" / "Source.md").write_text(
            "# Source\n\nSee [[Wiki/Target|target]] and [[Wiki/Target#Section]].\n"
        )
        (self.space / "Inbox").mkdir()
        (self.space / "Inbox" / "Other.md").write_text("# Other\n")
        (self.space / "Library").mkdir()
        (self.space / "Library" / "Hidden.md").write_text("[[Wiki/Target]]")
        (self.space / "CONFIG.md").write_text("# CONFIG\n")
        result = list_pages(self.space, "Wiki", limit=1)
        self.assertEqual(2, result["total"])
        self.assertEqual(1, len(result["pages"]))
        self.assertTrue(result["truncated"])
        links = backlinks(self.space, "Wiki/Target")
        self.assertEqual(2, links["total"])
        self.assertEqual(["Wiki/Source", "Wiki/Source"],
                         [row["page"] for row in links["results"]])
        with self.assertRaises(KnowledgeError):
            list_pages(self.space, "../etc")
        with self.assertRaises(KnowledgeError):
            backlinks(self.space, "../etc")


if __name__ == "__main__":
    unittest.main()
