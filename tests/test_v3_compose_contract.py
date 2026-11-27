"""Static security and install contracts for the new three-service add-on."""
from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class V3ComposeContractTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((ROOT / "compose.addon.yaml").read_text())
        self.manifest = yaml.safe_load((ROOT / "switchboard-addon.yaml").read_text())
        self.services = self.compose["services"]

    def test_every_service_is_namespaced_and_private(self):
        self.assertEqual(
            {"silverbulletapp", "silverbulletbootstrap", "silverbulletadapter"},
            set(self.services),
        )
        for name, service in self.services.items():
            with self.subTest(name=name):
                self.assertNotIn("ports", service)
                self.assertTrue(service["read_only"])
                self.assertEqual(["ALL"], service["cap_drop"])
                self.assertIn("no-new-privileges:true", service["security_opt"])
                self.assertEqual({"addons"}, set(service["networks"]))
                self.assertEqual(["/tmp"], service["tmpfs"])

    def test_3_slim_image_is_digest_pinned(self):
        app = self.services["silverbulletapp"]
        self.assertRegex(
            app["image"],
            r"^ghcr.io/silverbulletmd/silverbullet@sha256:[0-9a-f]{64}$",
        )
        self.assertNotIn("command", app)
        self.assertNotIn("SB_USER", app["environment"])
        self.assertNotIn("SB_AUTH_TOKEN", app["environment"])
        self.assertEqual("off", app["environment"]["SB_SHELL_BACKEND"])
        self.assertEqual("0", app["environment"]["SB_MCP"])

    def test_content_and_server_metadata_are_isolated(self):
        app = self.services["silverbulletapp"]
        destinations = {mount["target"] for mount in app["volumes"]}
        self.assertEqual({"/data", "/knowledge", "/reports"}, destinations)
        self.assertEqual({"server", "space", "reports"}, set(self.manifest["state_dirs"].split(",")))
        self.assertEqual({"token", "password", "agentpass", "mcptoken"},
                         set(self.manifest["secret_files"].split(",")))

    def test_bootstrap_must_finish_before_adapter_can_start(self):
        depend = self.services["silverbulletadapter"]["depends_on"]
        self.assertEqual(
            "service_completed_successfully",
            depend["silverbulletbootstrap"]["condition"],
        )
        self.assertEqual(
            ["python", "-m", "silverbullet_mcp.bootstrap"],
            self.services["silverbulletbootstrap"]["command"],
        )
        self.assertEqual("silverbulletadapter", self.manifest["service"])


if __name__ == "__main__":
    unittest.main()
