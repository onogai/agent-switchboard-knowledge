"""Disposable full SilverBullet add-on Compose acceptance on an ISOLATED Docker host.

Tests the actual schema-v2 packaging, two services, private network,
credential mounts and seven real HTTP MCP tools. NEVER points at production
data, production networks, production Core/Registry or host ports.

Operator-only. Can coexist with other Docker projects: creates and removes
only randomly named, disposable test resources. No production migration.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from addon_compose_policy import validate_v2


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT
ACCEPTANCE = ROOT / "scripts" / "check_silverbullet_http.py"


class AcceptanceError(RuntimeError):
    pass


def command(*args: str, cwd: Path | None = None, env: dict | None = None,
            input_text: str | None = None) -> str:
    # Do not echo raw Compose/exec errors: credential values can appear in
    # interpolation diagnostics or stack traces from the test container.
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, input=input_text,
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(f"{args[0]} failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise AcceptanceError(f"{args[0]} exited with status {result.returncode}")
    return result.stdout.strip()


def docker(*args: str, **kwargs) -> str:
    return command("docker", *args, **kwargs)


def _compose(source: Path, env_path: Path, project: str, *args: str) -> str:
    return docker(
        "compose", "--project-name", project, "--project-directory", str(source),
        "--env-file", str(env_path), "-f", str(source / "compose.addon.yaml"),
        *args,
    )


def _probe(container: str) -> None:
    code = """
import asyncio
import os
from pathlib import Path
from fastmcp import Client
async def main():
    token = Path(os.environ["SILVERBULLET_MCP_TOKEN_FILE"]).read_text().strip()
    async with Client("http://127.0.0.1:8773/mcp", timeout=5, auth=token) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {
            "notes_search", "notes_read_page", "notes_capture",
            "notes_create_wiki_page", "notes_update_page",
            "notes_list_agent_feedback", "notes_resolve_agent_feedback",
        }
asyncio.run(main())
"""
    docker("exec", "-i", container, "python", "-", input_text=code)


def _mcp_auth_probe(container: str) -> None:
    """Real HTTP 401 for missing/incorrect MCP service credential."""
    code = """
import httpx
url = "http://127.0.0.1:8773/mcp"
request = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "negative-auth-probe", "version": "1"},
    },
}
headers = {"Accept": "application/json, text/event-stream"}
with httpx.Client(timeout=6, trust_env=False) as client:
    anonymous = client.post(url, json=request, headers=headers)
    assert anonymous.status_code == 401, anonymous.status_code
    denied = client.post(url, json=request,
        headers={**headers, "Authorization": "Bearer invalid-candidate-token"})
    assert denied.status_code == 401, denied.status_code
"""
    docker("exec", "-i", container, "python", "-", input_text=code)


def _api_probe(container: str) -> None:
    code = """
import httpx, os
from pathlib import Path
base = os.environ["KNOWLEDGE_BASE_URL"]
token = Path(os.environ["KNOWLEDGE_TOKEN_FILE"]).read_text().strip()
with httpx.Client(timeout=8, trust_env=False) as client:
    anonymous = client.get(base + "/.fs/Wiki/E2E-Sentinel.md")
    assert anonymous.status_code in (401, 403), anonymous.status_code
    authorized = client.get(base + "/.fs/Wiki/E2E-Sentinel.md",
                            headers={"Authorization": "Bearer " + token})
    assert authorized.status_code == 200, authorized.status_code
    assert "Disposable add-on acceptance" in authorized.text
"""
    docker("exec", "-i", container, "python", "-", input_text=code)


def _accept(container: str) -> dict:
    source = ACCEPTANCE.read_text(encoding="utf-8")
    main_guard = '\nif __name__ == "__main__":'
    if main_guard not in source:
        raise AcceptanceError("Expected acceptance checker entrypoint not found")
    code = source.split(main_guard)[0] + """
import asyncio
from pathlib import Path
import os
result = asyncio.run(accept(
    "http://127.0.0.1:8773/mcp",
    read_page="Wiki/E2E-Sentinel",
    write_test=True,
    bearer_token=Path(os.environ["SILVERBULLET_MCP_TOKEN_FILE"]).read_text().strip(),
))
print("E2E_RESULT:" + json.dumps(result, sort_keys=True))
"""
    out = docker("exec", "-i", container, "python", "-", input_text=code)
    for line in reversed(out.splitlines()):
        if line.startswith("E2E_RESULT:"):
            data = json.loads(line.removeprefix("E2E_RESULT:"))
            if not (data.get("catalog_checked") and data.get("read_checked") and
                    data.get("write_checked") and data.get("etag_checked") and
                    data.get("disposable_pages_created") == 2):
                raise AcceptanceError("Incomplete seven-tool HTTP acceptance")
            return data
    raise AcceptanceError("Seven-tool MCP acceptance returned no result")


def _assert_unused_project(project: str, network: str) -> None:
    """Refuse collisions with this test's generated project/network.

    An occupied Docker daemon is fine; only resources belonging to our
    randomized namespace may be created or destroyed by this script.
    """
    used = docker("ps", "-aq", "--filter",
                  "label=com.docker.compose.project=" + project)
    networks = docker("network", "ls", "--format", "{{.Name}}",
                      "--filter", "name=^" + network + "$")
    if used.strip() or network in networks.splitlines():
        raise AcceptanceError("Randomized Docker E2E project/network already exists")



def _inspect(container: str, network: str, state: Path) -> None:
    info = json.loads(docker("inspect", container))[0]
    host = info["HostConfig"]
    if (host.get("PortBindings") or host.get("Privileged") or
        host.get("CapAdd") or not host.get("ReadonlyRootfs") or
        "ALL" not in (host.get("CapDrop") or [])):
        raise AcceptanceError("Container has unsafe host exposure or privileges")
    if set(info["NetworkSettings"]["Networks"]) != {network}:
        raise AcceptanceError("Container joined an unexpected Docker network")
    for mount in info["Mounts"]:
        src = Path(mount["Source"]).resolve()
        if src != state and state not in src.parents:
            raise AcceptanceError("Container mounted data outside disposable state")
        if mount["Destination"] in ("/knowledge", "/run/secrets/silverbullet-token", "/run/secrets/silverbullet-mcp-token") and mount["RW"]:
            raise AcceptanceError("MCP knowledge/token mounts must be read-only")


def _stage_package(source: Path) -> None:
    """Stage only build/Compose inputs, never local venv, state or secrets.

    The staging source is a Git checkout; allowlisting files prevents an
    operator's unrelated working-tree files entering the temporary context.
    """
    source.mkdir(mode=0o700)
    for name in (
        ".dockerignore", "Dockerfile", "requirements.txt",
        "compose.addon.yaml", "switchboard-addon.yaml",
    ):
        src = PACKAGE / name
        if src.is_symlink() or not src.is_file():
            raise AcceptanceError(f"Missing or unsafe add-on package file: {name}")
        shutil.copyfile(src, source / name)
    package = PACKAGE / "silverbullet_mcp"
    if package.is_symlink() or not package.is_dir():
        raise AcceptanceError("Missing or unsafe SilverBullet MCP package")
    dest = source / "silverbullet_mcp"
    dest.mkdir(mode=0o700)
    for file in sorted(package.glob("*.py")):
        if file.is_symlink() or not file.is_file():
            raise AcceptanceError("Unsafe Python source in SilverBullet MCP package")
        shutil.copyfile(file, dest / file.name)


def run() -> dict:
    if not PACKAGE.is_dir() or not ACCEPTANCE.is_file():
        raise AcceptanceError("Required staged package or acceptance module absent")
    # Preflight MUST precede all Docker network/build/container mutations.
    suffix = secrets.token_hex(6)
    project = "sbe2e" + suffix
    network = "sb-e2e-" + suffix
    _assert_unused_project(project, network)
    with tempfile.TemporaryDirectory(prefix="switchboard-silverbullet-addon-e2e-") as td:
        root = Path(td)
        source = root / "source"
        _stage_package(source)
        state = root / "state"
        state.mkdir(mode=0o700)
        space = state / "space"
        space.mkdir(mode=0o700)
        (space / "Wiki").mkdir(mode=0o700)
        (space / "Wiki" / "E2E-Sentinel.md").write_text(
            "# Disposable add-on acceptance\n", encoding="utf-8"
        )
        secret_dir = state / "secrets"
        secret_dir.mkdir(mode=0o700)
        token = secrets.token_urlsafe(48)
        password = secrets.token_urlsafe(48)
        mcp_token = secrets.token_urlsafe(48)
        for filename, value in (("token", token), ("mcptoken", mcp_token)):
            secret_file = secret_dir / filename
            secret_file.write_text(value + "\n", encoding="utf-8")
            secret_file.chmod(0o600)
        env_path = root / "config.env"
        env_path.write_text(
            f"SWITCHBOARD_ADDON_INSTANCE={project}\n"
            f"SWITCHBOARD_ADDON_STATE={state}\n"
            f"SWITCHBOARD_ADDONS_NETWORK={network}\n"
            f"HOST_UID={os.getuid()}\nHOST_GID={os.getgid()}\n"
            f"SWITCHBOARD_ADDON_SECRET_TOKEN={token}\n"
            f"SWITCHBOARD_ADDON_SECRET_PASSWORD={password}\n"
            f"SWITCHBOARD_ADDON_SECRET_MCPTOKEN={mcp_token}\n",
            encoding="utf-8",
        )
        env_path.chmod(0o600)
        network_created = False
        phase = "create temporary Docker network"
        try:
            docker("network", "create", "--internal", network)
            network_created = True
            phase = "render Docker Compose config"
            rendered = json.loads(_compose(source, env_path, project, "config", "--format", "json"))
            phase = "validate schema-v2 Compose policy"
            validate_v2(rendered, root, {
                "services": ["silverbulletapp", "silverbulletadapter"], "service": "silverbulletadapter",
                "instance": project, "addon_id": "silverbullet", "network": network,
            })
            phase = "build and start two Compose services"
            _compose(source, env_path, project, "up", "-d", "--build", "--wait", "silverbulletapp", "silverbulletadapter")
            started = True
            phase = "locate and inspect temporary containers"
            sb = _compose(source, env_path, project, "ps", "-q", "silverbulletapp")
            mcp = _compose(source, env_path, project, "ps", "-q", "silverbulletadapter")
            if not sb or not mcp:
                raise AcceptanceError("One or both add-on containers did not start")
            _inspect(sb, network, state)
            _inspect(mcp, network, state)
            phase = "wait for MCP initialize/tools/list"
            for attempt in range(50):
                try:
                    _probe(mcp)
                    break
                except AcceptanceError:
                    if attempt == 49:
                        raise AcceptanceError("MCP initialize/tools/list never became ready")
                    time.sleep(2)
            phase = "reject anonymous/invalid MCP bearer"
            _mcp_auth_probe(mcp)
            phase = "authenticate real SilverBullet filesystem API"
            _api_probe(mcp)
            phase = "exercise seven MCP tools, writes, feedback and ETags"
            result = _accept(mcp)
            phase = "verify persistent disposable Markdown writes"
            files = list(space.rglob("*.md"))
            if len(files) < 3:
                raise AcceptanceError("MCP writes not persisted in disposable Markdown space")
            return {
                "official_silverbullet": "2.12.1-slim",
                "compose_service_count": 2,
                "private_internal_network": True,
                "host_ports_published": False,
                "mcp_tools_tested": 7,
                "mcp_bearer_required": True,
                "mcp_anonymous_and_invalid_rejected": True,
                "read_write_etag_feedback_verified": True,
                "disposable_data_only": True,
                "production_registry_changed": False,
                "acceptance": result,
            }
        except Exception as exc:
            # All AcceptanceError text is authored and sanitized; never echo
            # raw subprocess stderr/stdout or untrusted exception messages.
            if isinstance(exc, AcceptanceError):
                raise AcceptanceError(f"{phase}: {exc}") from exc
            raise AcceptanceError(f"{phase}: {type(exc).__name__}") from exc
        finally:
            if network_created:
                # Project name and network name are randomized and created by
                # this invocation. Never select any pre-existing resources.
                # --rmi local removes only the project's locally built image.
                cleanup_errors = []
                try:
                    _compose(source, env_path, project, "down", "--remove-orphans", "--rmi", "local")
                except AcceptanceError:
                    cleanup_errors.append("Compose down")
                try:
                    docker("network", "rm", network)
                except AcceptanceError:
                    cleanup_errors.append("Docker network removal")
                if cleanup_errors:
                    print(
                        "WARNING: disposable E2E cleanup incomplete ("
                        + ", ".join(cleanup_errors) + "). "
                        "Inspect containers named " + project +
                        " and network " + network +
                        " before removing any resources. "
                        "Do NOT use docker system prune.",
                        file=sys.stderr,
                    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-disposable-test", action="store_true",
        help="Confirm this test may build temporary containers and files (no production data)",
    )
    args = parser.parse_args(argv)
    if not args.confirm_disposable_test:
        parser.error("Real Compose acceptance requires --confirm-disposable-test")
    try:
        result = run()
    except AcceptanceError as exc:
        parser.exit(2, f"Full add-on acceptance failed: {exc}\n")
    except Exception as exc:
        parser.exit(2, f"Full add-on acceptance failed ({type(exc).__name__})\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
