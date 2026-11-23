"""SilverBullet 3 HTTP client contract and security tests (no real server)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from silverbullet_mcp import v3_api
from silverbullet_mcp.knowledge import KnowledgeClient, KnowledgeError


class V3APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "token"
        self.token_file.write_text("example-private-token")
        self.client = KnowledgeClient("http://silverbullet.internal:3000", self.token_file,
                                      Path(self.temp.name))

    def fake_transport(self, *, status=200, data=None):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(status, json={} if data is None else data)
        return calls, httpx.MockTransport(handler)

    def test_help_bearer_and_bounded_schema(self):
        calls, transport = self.fake_transport(data={"version": 1, "outline": {"folders": []}})
        original_client = httpx.Client
        def factory(*args, **kwargs):
            return original_client(*args, transport=transport, **kwargs)
        with patch.object(KnowledgeClient, "from_environment", return_value=self.client), \
             patch.object(v3_api.httpx, "Client", side_effect=factory):
            self.assertEqual(1, v3_api.overview()["version"])
        self.assertEqual("/.help", calls[0].url.path)
        self.assertEqual("Bearer example-private-token", calls[0].headers["authorization"])

    def test_search_sends_exact_api_shape(self):
        calls, transport = self.fake_transport(data={"items": [], "total": 0})
        original_client = httpx.Client
        def factory(*args, **kwargs):
            return original_client(*args, transport=transport, **kwargs)
        with patch.object(KnowledgeClient, "from_environment", return_value=self.client), \
             patch.object(v3_api.httpx, "Client", side_effect=factory):
            v3_api.find("family car", "natural", 8)
        self.assertEqual("/.search/query", calls[0].url.path)
        self.assertEqual({"query": "family car", "mode": "natural",
                          "scope": "pages", "limit": 8},
                         json.loads(calls[0].content))

    def test_validation_precedes_http(self):
        for args in [("x", "words", 10), ("proper query", "invalid", 10),
                     ("proper query", "words", 100)]:
            with self.subTest(args=args), self.assertRaises(KnowledgeError):
                v3_api.find(*args)
        for page in ("../secrets", ".dashboard", "Wiki/../../oops"):
            with self.subTest(page=page), self.assertRaises(KnowledgeError):
                v3_api.related(page)

    def test_error_does_not_echo_token_or_response_body(self):
        calls, transport = self.fake_transport(status=401, data={"secret": "never-echo"})
        original_client = httpx.Client
        def factory(*args, **kwargs):
            return original_client(*args, transport=transport, **kwargs)
        with patch.object(KnowledgeClient, "from_environment", return_value=self.client), \
             patch.object(v3_api.httpx, "Client", side_effect=factory):
            with self.assertRaises(KnowledgeError) as err:
                v3_api.overview()
        self.assertNotIn("never-echo", str(err.exception))
        self.assertNotIn("example-private-token", str(err.exception))
    def test_search_busy_retries_twice_and_reports_busy_not_quota(self):
        calls = []
        original_client = httpx.Client
        def handler(request):
            calls.append(request)
            return (httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "busy"})
                    if len(calls) < 3 else httpx.Response(200, json={"items": [], "total": 0}))
        transport = httpx.MockTransport(handler)
        def factory(*args, **kwargs):
            return original_client(*args, transport=transport, **kwargs)
        with patch.object(KnowledgeClient, "from_environment", return_value=self.client), \
             patch.object(v3_api.httpx, "Client", side_effect=factory), \
             patch.object(v3_api.time, "sleep") as wait:
            self.assertEqual([], v3_api.find("sensible question", "natural", 5)["items"])
        self.assertEqual(3, len(calls))
        self.assertEqual(2, wait.call_count)

        calls.clear()
        transport = httpx.MockTransport(lambda request: (
            calls.append(request) or httpx.Response(
                429, headers={"Retry-After": "1"}, json={"error": "busy"})))
        with patch.object(KnowledgeClient, "from_environment", return_value=self.client), \
             patch.object(v3_api.httpx, "Client", side_effect=factory), \
             patch.object(v3_api.time, "sleep"):
            with self.assertRaisesRegex(KnowledgeError, "busy after bounded retries"):
                v3_api.find("sensible question", "natural", 5)
        self.assertEqual(3, len(calls))


if __name__ == "__main__":
    unittest.main()
