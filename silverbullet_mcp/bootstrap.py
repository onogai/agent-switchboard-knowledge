"""One-shot, idempotent SilverBullet 3.0 private-space provisioning.

Runs only on the add-on's internal Docker network. No unauthenticated host ports,
no OAuth, no copied production secrets, and no credentials in logs.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import time

import httpx

from .knowledge import KnowledgeError

ADMIN = "admin"
AGENT = "switchboard-agent"
SPACE = "Knowledge"
SPACE_FOLDER = "spaces/knowledge"
TOKEN_NAME = "switchboard-addon"
BASE = os.environ.get("KNOWLEDGE_BASE_URL", "").rstrip("/")
TIMEOUT_SECONDS = 90


def _secret(directory: Path, name: str) -> str:
    path = directory / name
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise KnowledgeError("Required bootstrap credential is not a private regular file.")
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise KnowledgeError("Required bootstrap credential is unavailable.") from exc
    if len(value) < 32 or not value.isascii() or not value.isprintable():
        raise KnowledgeError("Required bootstrap credential is empty or malformed.")
    return value


def _expect(response: httpx.Response, code: int = 200) -> dict:
    if response.status_code != code:
        raise KnowledgeError(f"SilverBullet provisioning returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise KnowledgeError("SilverBullet provisioning returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise KnowledgeError("SilverBullet provisioning returned an unexpected shape.")
    return payload


def _wait_for_app(http: httpx.Client) -> bool:
    """Return True if unconfigured, False if already initialized."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            r = http.get("/.setup/api/status")
            if r.status_code == 200:
                return True
            if r.status_code == 404:
                dashboard = http.get("/.dashboard/api/session")
                if dashboard.status_code in (200, 401):
                    return False
        except httpx.RequestError:
            pass
        time.sleep(0.7)
    raise KnowledgeError("SilverBullet 3.0 did not become ready for provisioning.")


def _login(http: httpx.Client, password: str) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            r = http.post("/.dashboard/api/login",
                          json={"username": ADMIN, "password": password})
            if r.status_code == 200 and r.json().get("status") == "ok":
                session = http.get("/.dashboard/api/session")
                if session.status_code == 200 and session.json().get("admin") is True:
                    return
        except (ValueError, httpx.RequestError):
            pass
        time.sleep(0.7)
    raise KnowledgeError("The generated administrator could not authenticate; refusing to change existing server.")


def _get(http: httpx.Client, endpoint: str) -> dict:
    return _expect(http.get(endpoint))


def _post(http: httpx.Client, endpoint: str, payload: dict) -> dict:
    return _expect(http.post(endpoint, json=payload))


def _patch(http: httpx.Client, endpoint: str, payload: dict) -> dict:
    return _expect(http.patch(endpoint, json=payload))


def _configure_agent(http: httpx.Client, agentpass: str) -> None:
    user_endpoint = f"/.dashboard/api/admin/users/{AGENT}"
    r = http.get(user_endpoint)
    if r.status_code == 404:
        _post(http, "/.dashboard/api/admin/users", {
            "username": AGENT, "password": agentpass, "admin": False,
        })
        user = _get(http, user_endpoint)
    else:
        user = _expect(r)
    if user.get("admin") is not False or user.get("disabled") is not False:
        raise KnowledgeError("Existing agent account has unexpected privileges or is disabled.")

    spaces = _get(http, "/.dashboard/api/admin/spaces")
    matches = [(ident, config) for ident, config in spaces.items()
               if isinstance(config, dict) and config.get("name") == SPACE]
    if len(matches) != 1:
        raise KnowledgeError("Expected exactly one SilverBullet Knowledge space.")
    ident, config = matches[0]
    if not re.fullmatch(r"[a-f0-9-]{36}", ident):
        raise KnowledgeError("Unrecognized SilverBullet space identifier.")
    if config.get("folder") != SPACE_FOLDER:
        raise KnowledgeError("Knowledge space folder is not the expected isolated directory.")
    if config.get("access", "none") != "none":
        raise KnowledgeError("Knowledge space would grant anonymous access.")
    binding = config.get("binding", {})
    if binding != {"prefix": "/"}:
        raise KnowledgeError("Knowledge space is not bound to the expected root path.")
    members = config.get("members") or {}
    expected = {"role": "write", "runtimeApi": False}
    if AGENT not in members:
        _patch(http, f"/.dashboard/api/admin/spaces/{ident}",
               {"members": {**members, AGENT: expected}})
    else:
        actual = members[AGENT]
        if actual.get("role") != "write" or actual.get("runtimeApi") is not False:
            raise KnowledgeError("Existing agent role is not restricted as expected.")
    current = _get(http, f"/.dashboard/api/admin/spaces/{ident}")
    member = (current.get("members") or {}).get(AGENT) or {}
    if (current.get("access", "none") != "none"
            or member.get("role") != "write"
            or member.get("runtimeApi") is not False):
        raise KnowledgeError("Knowledge space permissions failed verification.")


def _configure_token(http: httpx.Client, token_path: Path) -> None:
    current = _secret(token_path.parent, token_path.name)
    if current.startswith("sbt_"):
        check = http.get("/.help", headers={"Authorization": f"Bearer {current}"})
        if check.status_code == 200:
            _expect(check)
            return
        raise KnowledgeError("Existing SilverBullet agent API token failed authorization; refusing silent rotation.")

    user = _get(http, f"/.dashboard/api/admin/users/{AGENT}")
    if TOKEN_NAME in (user.get("tokens") or {}):
        raise KnowledgeError("Agent API token was previously issued but cannot be recovered from disk.")
    minted = _post(http, f"/.dashboard/api/admin/users/{AGENT}/tokens",
                   {"name": TOKEN_NAME}).get("token")
    if not isinstance(minted, str) or not re.fullmatch(r"sbt_[0-9a-f]{64}", minted):
        raise KnowledgeError("Server did not return a valid SilverBullet API token.")
    # Token existed as a private installer placeholder. Replace atomically
    # before the adapter starts (depends_on service_completed_successfully).
    tmp = token_path.with_name(token_path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(minted + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, token_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    check = http.get("/.help", headers={"Authorization": f"Bearer {minted}"})
    if check.status_code != 200:
        raise KnowledgeError("Minted agent API token cannot reach the Knowledge space.")
    _expect(check)


def run() -> None:
    if not BASE.startswith("http://addon-") or not BASE.endswith("-silverbulletapp:3000"):
        raise KnowledgeError("Bootstrap requires the private add-on SilverBullet endpoint.")
    directory = Path(os.environ.get("SILVERBULLET_BOOTSTRAP_SECRETS_DIR",
                                    "/run/addon-secrets"))
    if not directory.is_dir() or directory.is_symlink():
        raise KnowledgeError("Private bootstrap secrets directory is missing.")
    adminpass = _secret(directory, "password")
    agentpass = _secret(directory, "agentpass")
    _secret(directory, "token")  # installer-owned placeholder or server-minted token
    with httpx.Client(base_url=BASE, timeout=5, follow_redirects=False,
                      trust_env=False) as http:
        needs_setup = _wait_for_app(http)
        if needs_setup:
            response = http.post("/.setup/api/complete", json={
                "adminUsername": ADMIN, "adminPassword": adminpass,
                "space": {"name": SPACE, "prefix": "/",
                          "folder": SPACE_FOLDER,
                          "revisions": "unmanaged", "starter": {"type": "none"}},
            })
            _expect(response)
        _login(http, adminpass)
        _configure_agent(http, agentpass)
        _configure_token(http, directory / "token")


if __name__ == "__main__":
    try:
        run()
    except (KnowledgeError, httpx.HTTPError, OSError) as error:
        # Never print HTTP response bodies, request headers, or credentials.
        print(f"SilverBullet bootstrap failed: {type(error).__name__}", flush=True)
        raise SystemExit(1)
    print("SilverBullet bootstrap complete (private space, non-admin MCP token).", flush=True)
