import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from silverbullet_mcp.documents import query_pages
from silverbullet_mcp.knowledge import KnowledgeError, search_pages, KnowledgeClient, PageConflict
from silverbullet_mcp import v3_api


class DocumentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict("os.environ", {"KNOWLEDGE_SEARCH_EXCLUDE_PREFIXES": "Wiki/Reports"})
        env.start()
        self.addCleanup(env.stop)

    def put(self, name, body):
        path = self.root / (name + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_query_filters_projection_and_pagination(self):
        self.put("Wiki/Reports/News/a", "---\ndate: 2026-11-27\ntags: [news, morning]\n---\nFirst\n")
        self.put("Wiki/Reports/News/b", "---\ndate: '2026-11-26'\ntags: [news]\n---\nSecond\n")
        self.put("Wiki/Reports/News/c", "---\ndate: 2026-11-25\ntags: [news]\n---\nOld")
        self.put("Wiki/Reports/News/bad", "# No date")
        self.put("Wiki/Reports/Other/a", "---\ndate: 2026-11-27\ntags: [news]\n---\nOutside")
        args = (self.root, "Wiki/Reports/News", "2026-11-26", "2026-11-27")
        first = query_pages(*args, metadata_equals={"tags": "news"}, fields=["tags"],
                            include_content=True, limit=1)
        self.assertEqual(2, first["total"])
        self.assertEqual(1, first["skipped_invalid"])
        self.assertEqual(1, first["next_offset"])
        self.assertEqual({"page": "Wiki/Reports/News/a", "date": "2026-11-27",
                          "metadata": {"tags": ["news", "morning"]}, "content": "First\n"},
                         first["items"][0])
        second = query_pages(*args, offset=1, fields=[])
        self.assertEqual("Wiki/Reports/News/b", second["items"][0]["page"])
        self.assertNotIn("content", second["items"][0])
        self.assertIsNone(second["next_offset"])

    def test_budget_and_aliases(self):
        for name in ("a", "b"):
            self.put("Wiki/Reports/" + name, "---\ndate: 2026-11-27\n---\n" + "x" * 650)
        self.put("Wiki/Reports/alias", "---\ndate: 2026-11-27\nx: &x [1]\ny: *x\n---\n")
        args = (self.root, "Wiki/Reports", "2026-11-27", "2026-11-27")
        result = query_pages(*args, include_content=True, max_bytes=1100)
        self.assertEqual(1, len(result["items"]))
        self.assertLessEqual(len(json.dumps(result).encode()), 1100)
        self.assertEqual(1, result["next_offset"])
        self.assertEqual(1, result["skipped_invalid"])
        self.put("Wiki/Reports/0", "---\ndate: 2026-11-27\n---\n" + "x" * 2000)
        with self.assertRaisesRegex(KnowledgeError, "does not fit"):
            query_pages(*args, include_content=True, max_bytes=1000)

    def test_search_isolation_and_explicit_opt_in(self):
        self.put("Wiki/Reports/a", "garden")
        self.put("Wiki/ReportsArchive/a", "garden")
        self.assertEqual(1, search_pages(self.root, "garden")["total"])
        self.assertEqual(2, search_pages(self.root, "garden", include_excluded=True)["total"])
        client = KnowledgeClient("http://localhost", self.root / "token", self.root)
        with patch.object(KnowledgeClient, "from_environment", return_value=client), \
             patch.object(v3_api, "_api", return_value={"items": [{"page": "Wiki/Reports/stale"},
                                                                  {"page": "Wiki/ReportsArchive/a"}]}) as api:
            result = v3_api.find("garden", "natural")
            self.assertEqual(["Wiki/Reports/a"], api.call_args.kwargs["payload"]["exclude"])
            self.assertTrue(result["partial"])
            self.assertEqual(1, len(result["items"]))
            v3_api.related("Wiki/Example")
            self.assertEqual(["Wiki/Reports/a"], api.call_args.kwargs["payload"]["exclude"])
            self.assertEqual(2, len(v3_api.find("garden", include_excluded=True)["items"]))
            self.assertNotIn("exclude", api.call_args.kwargs["payload"])

    def test_idempotent_create_does_not_overwrite(self):
        client = KnowledgeClient("http://localhost", self.root / "token", self.root)
        with patch.object(client, "_request", side_effect=PageConflict("conflict")) as request, \
             patch.object(client, "read_page", return_value={"content": "same", "etag": "rev"}):
            result = client.create_page("Wiki/Reports/a", "same", idempotent=True)
            self.assertFalse(result["created"])
            with self.assertRaisesRegex(KnowledgeError, "different content"):
                client.create_page("Wiki/Reports/a", "different", idempotent=True)
            self.assertEqual(2, request.call_count)
            self.assertTrue(all(call.kwargs["create"] for call in request.call_args_list))
