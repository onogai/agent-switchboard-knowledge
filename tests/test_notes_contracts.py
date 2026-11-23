"""Offline checks for the standalone public MCP and local Markdown search."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from silverbullet_mcp.server import mcp, notes_capture, notes_update_page
from silverbullet_mcp.knowledge import search_pages, list_feedback, KnowledgeError


EXPECTED = {
    "notes_search", "notes_read_page", "notes_capture",
    "notes_create_wiki_page", "notes_update_page",
    "notes_list_agent_feedback", "notes_resolve_agent_feedback",
    "notes_overview", "notes_search_status", "notes_search_ranked", "notes_related_pages",
}


class NotesContractTests(unittest.TestCase):
    def test_exact_clean_mcp_names_have_useful_agent_descriptions(self):
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual({t.name for t in tools}, EXPECTED)
        for t in tools:
            self.assertTrue(len(t.description) >= 85, t.name)
            self.assertTrue(t.name.startswith("notes_"), t.name)

    def test_offline_search_and_agent_feedback_do_not_require_core(self):
        with tempfile.TemporaryDirectory() as root:
            space = Path(root)
            (space / "Wiki").mkdir()
            (space / "Wiki" / "Example.md").write_text(
                "# Motor notes\n\nElectric motor torque is 100 Nm.\n\n"
                "<!-- @agent Check torque -->\n", encoding="utf-8"
            )
            result = search_pages(space, "torque", limit=10)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["results"][0]["page"], "Wiki/Example")
            feedback = list_feedback(space, limit=10)
            self.assertEqual(feedback["total"], 1)
            self.assertIn("Check torque", feedback["results"][0]["comment"])

    def test_writes_explicitly_denied_unless_deployment_enables_them(self):
        with patch.dict("os.environ", {"SILVERBULLET_MCP_WRITE_ENABLED": "false"}):
            with self.assertRaisesRegex(KnowledgeError, "disabled"):
                asyncio.run(notes_capture("Title", "Body"))
            with self.assertRaisesRegex(KnowledgeError, "disabled"):
                asyncio.run(notes_update_page("Wiki/Test", "Body", '"sha256:' + "a" * 64 + '"'))


if __name__ == "__main__":
    unittest.main()
