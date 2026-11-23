"""Disposable full SilverBullet add-on Compose acceptance on an ISOLATED Docker host.

Tests the actual schema-v2 packaging, three services, private network,
automatic bootstrap and eleven real HTTP MCP tools. NEVER points at production
data, production networks, production Core/Registry or host ports.

Operator-only. Can coexist with other Docker projects: creates and removes
only randomly named, disposable test resources. No production migration.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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


def _safe_docker_failure(stderr: str) -> str:
    """Classify known Compose/daemon failures without leaking stderr or secrets."""
    message = stderr.casefold()
    patterns = (
        ("dependency failed to start", "Compose dependency could not start"),
        ("didn't complete successfully", "one-shot dependency exited unsuccessfully"),
        ("invalid mount config", "Docker rejected a bind mount"),
        ("not a directory", "bind mount source/target type mismatch"),
        ("permission denied", "Docker or mounted file access denied"),
        ("no such image", "required Docker image is unavailable"),
        ("failed to create task", "container runtime failed to create task"),
        ("unknown flag", "Docker Compose rejected an option"),
    )
    for substring, label in patterns:
        if substring in message:
            return " (" + label + ")"
    return ""


def command(*args: str, cwd: Path | None = None, env: dict | None = None,
            input_text: str | None = None,
            timeout_s: int = 180) -> str:
    # Do not echo raw Compose/exec errors: credential values can appear in
    # interpolation diagnostics or stack traces from the test container.
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, input=input_text,
            capture_output=True, text=True, timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(f"{args[0]} exceeded {timeout_s}s" if isinstance(exc, subprocess.TimeoutExpired)
                              else f"{args[0]} failed: {type(exc).__name__}") from exc
    if result.returncode:
        # Never surface raw daemon/Compose stderr: it may include secrets from
        # interpolation or a failing command's arguments.
        diagnostic = _safe_docker_failure(result.stderr) if args[0] == "docker" else ""
        # This opt-in MCP checker prints only fixed operation labels and exception
        # class names, never raw tool errors, page content or credentials.
        if args[:3] == ("docker", "exec", "-i"):
            safe_steps = {
                "connect", "catalog", "read-sentinel", "create-wiki",
                "read-created", "filesystem-search", "list-feedback",
                "resolve-feedback", "update-page", "reject-stale-etag",
                "read-after-stale", "capture-note", "complete",
            }
            markers = re.findall(
                r"(?m)^E2E_ACCEPT_FAIL:([a-z-]{1,40}):([A-Za-z][A-Za-z0-9_]{0,50})$",
                result.stdout,
            )
            if markers:
                stage, kind = markers[-1]
                if stage in safe_steps:
                    diagnostic = f" (MCP acceptance failed at {stage}: {kind})"
        raise AcceptanceError(f"{args[0]} exited with status {result.returncode}{diagnostic}")
    return result.stdout.strip()


def docker(*args: str, **kwargs) -> str:
    return command("docker", *args, **kwargs)


def _compose(source: Path, env_path: Path, project: str, *args: str,
             timeout_s: int = 180) -> str:
    return docker(
        "compose", "--project-name", project, "--project-directory", str(source),
        "--env-file", str(env_path), "-f", str(source / "compose.addon.yaml"),
        *args, timeout_s=timeout_s,
    )


def _bootstrap_failure(container: str) -> str:
    """Extract only our allowlisted diagnostic marker, never raw container logs.

    Docker logs might contain credentials from unexpected upstream failure
    messages, so the acceptance tool must not print logs verbatim.
    """
    try:
        proc = subprocess.run(["docker", "logs", "--tail", "80", container],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "Bootstrap exited unsuccessfully (logs unavailable)"
    combined = (proc.stdout + "\n" + proc.stderr)[-24000:]
    markers = re.findall(
        r"SilverBullet bootstrap failed at ([a-z][a-z-]{0,60}): ([A-Za-z][A-Za-z0-9_]{0,60})",
        combined,
    )
    if markers:
        phase, name = markers[-1]
        return f"Bootstrap failed during {phase} ({name})"
    last_phase = re.findall(r"SilverBullet bootstrap phase: ([a-z][a-z-]{0,60})", combined)
    if last_phase:
        return f"Bootstrap stopped during {last_phase[-1]} (see host Docker logs)"
    return "Bootstrap exited unsuccessfully (no safe diagnostic marker found)"


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
            "notes_overview", "notes_search_status", "notes_search_ranked",
            "notes_related_pages",
        }
asyncio.run(main())
"""
    docker("exec", "-i", container, "python", "-", input_text=code)


def _v3_search_probe(container: str) -> None:
    """Verify actual SilverBullet 3 discovery and server-side full-text index."""
    code = """
import asyncio, os, json
from pathlib import Path
from fastmcp import Client

def decode(result):
    if result.is_error:
        raise ValueError("SilverBullet 3 MCP call failed")
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data

async def main():
    token = Path(os.environ["SILVERBULLET_MCP_TOKEN_FILE"]).read_text().strip()
    async with Client("http://127.0.0.1:8773/mcp", timeout=25, auth=token) as client:
        overview = decode(await client.call_tool("notes_overview", {}))
        assert overview["version"] == 1
        assert "outline" in overview
        status = decode(await client.call_tool("notes_search_status", {}))
        assert isinstance(status["capabilities"], dict)
        match = None
        for _ in range(15):
            result = decode(await client.call_tool("notes_search_ranked", {
                "query": "Disposable add-on acceptance", "mode": "phrase", "limit": 10,
            }))
            if any(row["page"] == "Wiki/E2E-Sentinel" for row in result["items"]):
                match = result
                break
            await asyncio.sleep(1)
        assert match is not None, "Indexed search missed disposable knowledge"
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
    anonymous = client.get(base + "/.fs/Wiki/E2E-Sentinel.md",
                           headers={"X-Sync-Mode": "true"})
    assert anonymous.status_code in (401, 403), anonymous.status_code
    authorized = client.get(base + "/.fs/Wiki/E2E-Sentinel.md",
                            headers={"Authorization": "Bearer " + token,
                                     "X-Sync-Mode": "true"})
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
_state = {"step": "connect"}
def _checkpoint(step):
    _state["step"] = step
    print("E2E_ACCEPT_STEP:" + step, flush=True)
try:
    result = asyncio.run(accept(
        "http://127.0.0.1:8773/mcp",
        read_page="Wiki/E2E-Sentinel",
        write_test=True,
        bearer_token=Path(os.environ["SILVERBULLET_MCP_TOKEN_FILE"]).read_text().strip(),
        on_step=_checkpoint,
    ))
except Exception as exc:
    print(f"E2E_ACCEPT_FAIL:{_state['step']}:{type(exc).__name__}", flush=True)
    raise SystemExit(2)
print("E2E_RESULT:" + json.dumps(result, sort_keys=True))
"""
    out = docker("exec", "-i", container, "python", "-", input_text=code)
    for line in reversed(out.splitlines()):
        if line.startswith("E2E_RESULT:"):
            data = json.loads(line.removeprefix("E2E_RESULT:"))
            if not (data.get("catalog_checked") and data.get("read_checked") and
                    data.get("write_checked") and data.get("etag_checked") and
                    data.get("disposable_pages_created") == 2):
                raise AcceptanceError("Incomplete eleven-tool HTTP acceptance")
            return data
    raise AcceptanceError("Eleven-tool MCP acceptance returned no result")


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



def _inspect(container: str, network: str, state: Path, *,
             readonly_targets: frozenset[str] = frozenset()) -> None:
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
        if mount["Destination"] in readonly_targets and mount["RW"]:
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
        server = state / "server"
        server.mkdir(mode=0o700)
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
        for filename, value in (("token", token), ("password", password),
                                ("agentpass", secrets.token_urlsafe(48)),
                                ("mcptoken", mcp_token)):
            secret_file = secret_dir / filename
            secret_file.write_text(value + "\n", encoding="utf-8")
            secret_file.chmod(0o600)
        env_path = root / "config.env"
        env_path.write_text(
            f"SWITCHBOARD_ADDON_INSTANCE={project}\n"
            f"SWITCHBOARD_ADDON_STATE={state}\n"
            f"SWITCHBOARD_ADDONS_NETWORK={network}\n"
            f"HOST_UID={os.getuid()}\nHOST_GID={os.getgid()}\n",
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
                "services": ["silverbulletapp", "silverbulletbootstrap", "silverbulletadapter"],
                "service": "silverbulletadapter",
                "instance": project, "addon_id": "silverbullet", "network": network,
            })
            phase = "pull pinned SilverBullet 3 slim image"
            print("E2E: " + phase, flush=True)
            _compose(source, env_path, project, "pull", "silverbulletapp", timeout_s=900)
            phase = "build standalone MCP and bootstrap images"
            print("E2E: " + phase, flush=True)
            _compose(source, env_path, project, "build", "silverbulletbootstrap",
                     "silverbulletadapter", timeout_s=900)
            phase = "start SilverBullet 3 server"
            print("E2E: " + phase, flush=True)
            _compose(source, env_path, project, "up", "-d", "--no-build",
                     "silverbulletapp", timeout_s=180)
            sb = _compose(source, env_path, project, "ps", "-q", "silverbulletapp")
            if not sb:
                raise AcceptanceError("SilverBullet server container was not created")
            _inspect(sb, network, state)
            phase = "start SilverBullet 3 bootstrap"
            print("E2E: " + phase, flush=True)
            _compose(source, env_path, project, "up", "-d", "--no-build",
                     "silverbulletbootstrap", timeout_s=180)
            init = _compose(source, env_path, project, "ps", "-a", "-q", "silverbulletbootstrap")
            if not init:
                raise AcceptanceError("Bootstrap container was not created")
            _inspect(init, network, state)
            phase = "wait for one-shot bootstrap to exit successfully"
            print("E2E: " + phase, flush=True)
            for attempt in range(210):
                init_state = json.loads(docker("inspect", init))[0]["State"]
                if init_state.get("Status") == "exited":
                    if init_state.get("ExitCode") != 0:
                        raise AcceptanceError(_bootstrap_failure(init))
                    break
                if init_state.get("Status") in {"dead", "removing"}:
                    raise AcceptanceError("Bootstrap container stopped unexpectedly")
                if attempt == 209:
                    raise AcceptanceError("Bootstrap did not complete within 210 seconds")
                time.sleep(1)
            phase = "start authenticated adapter after bootstrap"
            print("E2E: " + phase, flush=True)
            # The one-shot bootstrap already exited successfully. Starting
            # dependencies again can re-run setup and make this step fail even
            # when the adapter itself is healthy. Production Compose still
            # declares and enforces the dependency on a normal full startup.
            _compose(source, env_path, project, "up", "-d", "--no-build",
                     "--no-deps", "silverbulletadapter", timeout_s=180)
            mcp = _compose(source, env_path, project, "ps", "-q", "silverbulletadapter")
            if not mcp:
                raise AcceptanceError("MCP adapter was not created")
            _inspect(mcp, network, state, readonly_targets=frozenset({
                "/knowledge", "/run/secrets/silverbullet-token",
                "/run/secrets/silverbullet-mcp-token",
            }))
            if not (server / "users.json").is_file() or not (server / "spaces.json").is_file():
                raise AcceptanceError("Dashboard was not initialized")
            if not (secret_dir / "token").read_text().strip().startswith("sbt_"):
                raise AcceptanceError("Server-issued API token was not persisted")
            users = json.loads((server / "users.json").read_text())
            if users.get("admin", {}).get("admin") is not True or \
                    users.get("switchboard-agent", {}).get("admin") is not False:
                raise AcceptanceError("Missing restricted agent identity")
            spaces = json.loads((server / "spaces.json").read_text())
            entries = spaces.get("spaces", spaces)
            if not any(v.get("name") == "Knowledge" for v in entries.values() if isinstance(v, dict)):
                raise AcceptanceError("Knowledge space was not initialized")
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
            phase = "verify SilverBullet 3 overview and indexed full-text search"
            print("E2E: " + phase, flush=True)
            _v3_search_probe(mcp)
            phase = "exercise eleven MCP tools, writes, feedback and ETags"
            result = _accept(mcp)
            phase = "verify persistent disposable Markdown writes"
            files = list(space.rglob("*.md"))
            if len(files) < 3:
                raise AcceptanceError("MCP writes not persisted in disposable Markdown space")
            # The Switchboard host installer uses a single Compose up --wait
            # for every service, including the completed one-shot bootstrap.
            # Reproduce that exact lifecycle on disposable data before any
            # user tries installing the add-on through the production CLI.
            phase = "verify Switchboard installer-style Compose up --wait"
            print("E2E: " + phase, flush=True)
            _compose(source, env_path, project, "up", "-d", "--no-build",
                     "--wait", "silverbulletapp", "silverbulletbootstrap",
                     "silverbulletadapter", timeout_s=240)
            phase = "verify MCP remains available after installer-style startup"
            _probe(mcp)
            return {
                "official_silverbullet": "3.0-edge-slim (digest pinned)",
                "compose_service_count": 3,
                "bootstrap_complete": True,
                "installer_style_compose_wait_verified": True,
                "private_internal_network": True,
                "host_ports_published": False,
                "mcp_tools_tested": 11,
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
