"""MCP service bearer authentication: private secret, constant-time verification."""
import asyncio
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from silverbullet_mcp.server import (
    PrivateBearerVerifier, _authentication,
)


class SilverBulletMCPServiceAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secret = Path(self.temp.name) / "mcptoken"
        self.value = "a" * 72
        self.secret.write_text(self.value + "\n", encoding="utf-8")
        self.secret.chmod(0o600)

    def test_real_asgi_mcp_rejects_missing_or_wrong_bearer_with_401(self):
        import httpx
        from fastmcp import FastMCP
        mcp = FastMCP("Auth test", auth=PrivateBearerVerifier(self.secret))
        app = mcp.http_app()
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "auth-smoke", "version": "1"},
            },
        }
        async def check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
                timeout=5,
            ) as client:
                for auth in (None, "Bearer incorrect-token"):
                    headers = {
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    }
                    if auth:
                        headers["Authorization"] = auth
                    response = await client.post("/mcp", json=request,
                                                 headers=headers)
                    self.assertEqual(response.status_code, 401)
        asyncio.run(check())

    def test_valid_token_has_expected_service_identity(self):
        verifier = PrivateBearerVerifier(self.secret)
        accepted = asyncio.run(verifier.verify_token(self.value))
        self.assertEqual(accepted.client_id, "switchboard-core")
        self.assertEqual(accepted.subject, "switchboard-core")
        self.assertEqual(accepted.scopes, [])
        self.assertEqual(accepted.token, self.value)
        self.assertIsNone(asyncio.run(verifier.verify_token(self.value + "X")))
        self.assertIsNone(asyncio.run(verifier.verify_token("wrong")))

    def test_fails_closed_without_private_token_on_network_binding(self):
        with patch.dict(os.environ, {"SILVERBULLET_MCP_BIND_HOST": "0.0.0.0"},
                        clear=True):
            with self.assertRaisesRegex(ValueError, "requires SILVERBULLET_MCP_TOKEN_FILE"):
                _authentication()

    def test_stage_can_continue_loopback_anonymous(self):
        with patch.dict(os.environ, {"SILVERBULLET_MCP_BIND_HOST": "127.0.0.1"},
                        clear=True):
            self.assertIsNone(_authentication())

    def test_network_binding_accepts_only_private_file_token(self):
        with patch.dict(os.environ, {
            "SILVERBULLET_MCP_BIND_HOST": "0.0.0.0",
            "SILVERBULLET_MCP_TOKEN_FILE": str(self.secret),
        }, clear=True):
            self.assertIsInstance(_authentication(), PrivateBearerVerifier)

    def test_rejects_world_readable_and_symlinked_or_short_tokens(self):
        self.secret.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            PrivateBearerVerifier(self.secret)
        self.secret.chmod(0o600)
        symlink = Path(self.temp.name) / "link"
        symlink.symlink_to(self.secret)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            PrivateBearerVerifier(symlink)
        self.secret.write_text("weak-token\n")
        with self.assertRaisesRegex(ValueError, "too short"):
            PrivateBearerVerifier(self.secret)

    def test_issuer_is_not_silverbullet_api_token(self):
        from silverbullet_mcp import server
        with patch.dict(os.environ, {
            "SILVERBULLET_MCP_TOKEN_FILE": str(self.secret),
            "KNOWLEDGE_TOKEN_FILE": "/run/secrets/silverbullet-token",
        }, clear=True):
            self.assertNotIn("KNOWLEDGE_TOKEN_FILE", server._authentication.__code__.co_names)

    def test_secret_not_exposed_in_error(self):
        self.secret.write_text("password-" + "x" * 100 + "\n")
        self.secret.chmod(0o644)
        with self.assertRaises(ValueError) as error:
            PrivateBearerVerifier(self.secret)
        self.assertNotIn("password-", str(error.exception))


if __name__ == "__main__":
    unittest.main()
