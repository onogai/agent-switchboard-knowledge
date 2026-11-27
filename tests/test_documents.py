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

    def test_space_routing_and_native_isolation(self):
        import httpx
        reports = self.root / "reports"
        knowledge = self.root / "knowledge"
        reports.mkdir()
        knowledge.mkdir()
        (reports / "report.md").write_text("garden", encoding="utf-8")
        (knowledge / "context.md").write_text("garden", encoding="utf-8")
        token = self.root / "token"
        token.write_text("test-token")
        with patch.dict("os.environ", {"KNOWLEDGE_SPACE_DIR": str(knowledge),
                                      "REPORTS_SPACE_DIR": str(reports),
                                      "KNOWLEDGE_BASE_URL": "http://localhost:3000",
                                      "KNOWLEDGE_TOKEN_FILE": str(token)}):
            self.assertEqual("context", search_pages(KnowledgeClient.from_environment().root,
                                                     "garden")["results"][0]["page"])
            self.assertEqual("report", search_pages(KnowledgeClient.from_environment("reports").root,
                                                    "garden")["results"][0]["page"])
            calls = []
            def handle(request):
                calls.append(request)
                if request.url.path.endswith("/.search/query"):
                    return httpx.Response(200, json={"items": []})
                return httpx.Response(200, content=b"body", headers={"etag": "rev"})
            original = httpx.Client
            with patch.object(httpx, "Client", side_effect=lambda *a, **kw:
                              original(*a, transport=httpx.MockTransport(handle), **kw)):
                v3_api.find("garden")
                v3_api.find("garden", space="reports")
                KnowledgeClient.from_environment("reports").create_page("News/a", "body")
                KnowledgeClient.from_environment("reports").read_page("News/a")
            self.assertEqual(["/.search/query", "/reports/.search/query",
                              "/reports/.fs/News/a.md", "/reports/.fs/News/a.md"],
                             [r.url.path for r in calls])
            self.assertTrue(all(r.headers["authorization"] == "Bearer test-token" for r in calls))
            self.assertNotIn("exclude", json.loads(calls[0].content))
            with self.assertRaises(KnowledgeError):
                KnowledgeClient.from_environment("unknown")
            with self.assertRaises(KnowledgeError):
                KnowledgeClient.from_environment().create_page("News/a", "body")
            with self.assertRaises(KnowledgeError):
                KnowledgeClient.from_environment("reports").create_page("Library/Config", "body")

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
