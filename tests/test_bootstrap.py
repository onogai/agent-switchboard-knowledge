"""Offline SilverBullet 3 bootstrap contract using a fake HTTP server."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from silverbullet_mcp import bootstrap
from silverbullet_mcp.knowledge import KnowledgeError


class FakeV3:
    def __init__(self):
        self.setup = False
        self.users = {}
        self.spaces = {}
        self.tokens = {}
        self.setup_count = 0
        self.mint_count = 0

    def handle(self, req):
        path, method = req.url.path, req.method
        def ok(body, status=200, headers=None):
            return httpx.Response(status, json=body, headers=headers or {})
        payload = json.loads(req.content) if req.content else {}
        if path == "/.setup/api/status":
            return ok({"root": "/data"}) if not self.setup else ok({}, 404)
        if path == "/.setup/api/complete" and method == "POST":
            assert payload["space"]["folder"] == "spaces/knowledge"
            assert payload["space"]["starter"] == {"type": "none"}
            self.setup = True
            self.setup_count += 1
            self.users["admin"] = {"admin": True, "disabled": False, "tokens": {}}
            self.spaces["00000000-0000-4000-8000-000000000001"] = {
                "name": "Knowledge", "folder": "spaces/knowledge",
                "binding": {"prefix": "/"}, "access": "none", "members": {},
            }
            return ok({"status": "ok"})
        if path == "/.dashboard/api/login" and method == "POST":
            if self.setup and payload.get("username") == "admin":
                return ok({"status": "ok"}, headers={"set-cookie": "session=safe; Path=/; HttpOnly"})
            return ok({"status": "error"})
        if path == "/.dashboard/api/session":
            return ok({"admin": True}, 200) if "session=safe" in req.headers.get("cookie", "") else ok({}, 401)
        if path == "/.help":
            token = req.headers.get("authorization", "").removeprefix("Bearer ")
            if self.tokens.get(token) == "switchboard-agent":
                return ok({"version": 1, "outline": {"folders": []}})
            return ok({}, 401)
        prefix = "/.dashboard/api/admin/"
        if not path.startswith(prefix) or "session=safe" not in req.headers.get("cookie", ""):
            return ok({}, 401)
        endpoint = path[len(prefix):]
        if endpoint == "users" and method == "POST":
            self.users[payload["username"]] = {
                "admin": payload.get("admin", False), "disabled": False, "tokens": {},
            }
            return ok({"status": "ok"})
        if endpoint == "spaces" and method == "GET":
            return ok(self.spaces)
        if endpoint.startswith("spaces/"):
            ident = endpoint.split("/")[1]
            if method == "PATCH":
                self.spaces[ident]["members"] = payload["members"]
                return ok({"status": "ok"})
            if method == "GET":
                return ok(self.spaces[ident])
        if endpoint.startswith("users/"):
            parts = endpoint.split("/")
            user = parts[1]
            if method == "GET":
                return ok(self.users[user]) if user in self.users else ok({}, 404)
            if len(parts) == 3 and parts[2] == "tokens" and method == "POST":
                if payload["name"] in self.users[user]["tokens"]:
                    return ok({}, 400)
                token = "sbt_" + "a" * 64
                self.users[user]["tokens"][payload["name"]] = {"createdAt": "2026"}
                self.tokens[token] = user
                self.mint_count += 1
                return ok({"token": token})
        return ok({}, 404)


class SilverBullet3BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secrets = Path(self.tmp.name)
        for key, value in (
            ("password", "admin-secret-" + "p"*48),
            ("agentpass", "agent-secret-" + "p"*48),
            ("token", "installer-placeholder-" + "t"*48),
        ):
            f = self.secrets / key
            f.write_text(value)
            f.chmod(0o600)
        self.backend = FakeV3()
        transport = httpx.MockTransport(self.backend.handle)
        original = httpx.Client
        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)
        self.mock_client = patch.object(bootstrap.httpx, "Client", side_effect=factory)
        self.mock_client.start()
        self.addCleanup(self.mock_client.stop)
        self.mock_base = patch.object(bootstrap, "BASE", "http://addon-test-silverbulletapp:3000")
        self.mock_base.start()
        self.addCleanup(self.mock_base.stop)
        self.mock_env = patch.dict(os.environ, {
            "SILVERBULLET_BOOTSTRAP_SECRETS_DIR": str(self.secrets),
        })
        self.mock_env.start()
        self.addCleanup(self.mock_env.stop)

    def test_fresh_install_is_private_and_repeatable(self):
        bootstrap.run()
        self.assertEqual(1, self.backend.setup_count)
        self.assertEqual(1, self.backend.mint_count)
        space = next(iter(self.backend.spaces.values()))
        self.assertEqual("none", space["access"])
        self.assertEqual({"role": "write", "runtimeApi": False},
                         space["members"]["switchboard-agent"])
        self.assertEqual("sbt_" + "a"*64, (self.secrets / "token").read_text().strip())
        bootstrap.run()
        self.assertEqual(1, self.backend.setup_count)
        self.assertEqual(1, self.backend.mint_count)

    def test_invalid_existing_api_token_fails_closed(self):
        bootstrap.run()
        self.backend.tokens.clear()
        with self.assertRaisesRegex(KnowledgeError, "refusing silent rotation"):
            bootstrap.run()
        self.assertEqual(1, self.backend.mint_count)

    def test_untrusted_secret_file_permissions_rejected(self):
        (self.secrets / "password").chmod(0o644)
        with self.assertRaisesRegex(KnowledgeError, "private regular file"):
            bootstrap.run()

    def test_unexpected_space_access_is_never_relaxed_silently(self):
        bootstrap.run()
        next(iter(self.backend.spaces.values()))["access"] = "write"
        with self.assertRaisesRegex(KnowledgeError, "anonymous access"):
            bootstrap.run()


if __name__ == "__main__":
    unittest.main()
