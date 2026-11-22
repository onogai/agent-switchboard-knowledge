"""Strict host-side validation of schema-v2 multi-service add-on Compose.

This is a policy check for *trusted repositories*, not a container sandbox.
No arbitrary host paths, ports, extra networks or elevated privileges.
"""
from __future__ import annotations

from pathlib import Path
import re


class ComposePolicyError(ValueError):
    pass


def _fail(message: str) -> None:
    raise ComposePolicyError(message)


def validate_v2(data: dict, instance: Path, info: dict) -> None:
    services = data.get("services", {})
    expected = set(info["services"])
    if set(services) != expected or info["service"] not in expected or not 2 <= len(expected) <= 8:
        _fail("Manifest/Compose service inventory mismatch (2-8 services required)")
    nets = data.get("networks", {})
    if (set(nets) != {"addons"} or
        nets["addons"].get("name") != info["network"] or
        nets["addons"].get("external") is not True):
        _fail("Only the installer-selected external private network is allowed")

    state = (instance / "state").resolve(strict=True)
    for name, service in services.items():
        # Compose automatically registers each service key as a DNS alias on
        # its external network. A generic key like 'mcp' would collide with
        # Switchboard Core's own service. Require namespaced v2 service keys.
        addon_id = info.get("addon_id")
        if addon_id and not name.startswith(addon_id):
            _fail(f"Service {name} must be prefixed with add-on id {addon_id}")
        expected_alias = (
            "addon-" + info["instance"] if name == info["service"]
            else "addon-" + info["instance"] + "-" + name
        )
        aliases = (service.get("networks", {}).get("addons") or {}).get("aliases", [])
        if aliases != [expected_alias]:
            _fail(f"Service {name} must use only its installer-scoped DNS alias")
        forbidden = (
            "ports", "privileged", "devices", "cap_add", "network_mode", "pid",
            "ipc", "volumes_from", "extra_hosts", "links", "secrets", "configs",
            "extends", "profiles", "network_mode", "cgroup_parent",
            "uts", "userns_mode", "sysctls", "security_opt_placeholder",
        )
        if any(service.get(key) for key in forbidden):
            _fail(f"Service {name} requests unsupported host exposure or isolation bypass")
        uid = str(service.get("user", ""))
        parts = uid.split(":")
        if len(parts) != 2 or not all(part.isdecimal() and int(part) > 0 for part in parts):
            _fail(f"Service {name} must use a non-root numeric UID:GID")
        if service.get("read_only") is not True:
            _fail(f"Service {name} must have read_only root filesystem")
        if "no-new-privileges:true" not in service.get("security_opt", []):
            _fail(f"Service {name} must set no-new-privileges")
        if "ALL" not in service.get("cap_drop", []):
            _fail(f"Service {name} must drop all capabilities")
        if set(service.get("networks", {})) != {"addons"}:
            _fail(f"Service {name} must use only private addons network")
        if service.get("entrypoint") or service.get("command") and not service.get("image") and not service.get("build"):
            _fail(f"Service {name} has an unsupported entrypoint/command")
        build = service.get("build")
        image = service.get("image")
        if bool(build) == bool(image):
            # Compose can contain both build and image, but we deliberately
            # require exactly one provenance path for easy review.
            _fail(f"Service {name} must specify exactly one of build or image")
        if build:
            context = Path(build.get("context", "")).resolve()
            if context != (instance / "source").resolve():
                _fail(f"Service {name} build context must be the cloned source root")
            dockerfile = build.get("dockerfile", "Dockerfile")
            if not re.fullmatch(r"[A-Za-z0-9_./-]{1,140}", dockerfile) or dockerfile.startswith(("/", ".")) or ".." in Path(dockerfile).parts:
                _fail(f"Service {name} Dockerfile path is unsafe")
        else:
            # Full immutable image digest preferred; explicit SemVer tag is
            # allowed for official pinned upstream releases (e.g. 2.12.1-slim).
            if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*(?::[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?|@sha256:[a-f0-9]{64})", image):
                _fail(f"Service {name} must use a pinned image version or sha256 digest")
        mounts = service.get("volumes", [])
        if not mounts:
            _fail(f"Service {name} must explicitly declare state mounts")
        targets = set()
        for mount in mounts:
            if mount.get("type") != "bind" or not mount.get("source") or not mount.get("target"):
                _fail(f"Service {name} may only use explicit bind mounts")
            source = Path(mount["source"])
            if not source.is_absolute() or source.is_symlink():
                _fail(f"Service {name} has unsafe bind source")
            resolved = source.resolve(strict=False)
            if state not in resolved.parents and resolved != state:
                _fail(f"Service {name} may mount only its own add-on state")
            if not resolved.exists():
                _fail(f"Service {name} mount source does not exist")
            # Check every existing ancestor (including the source) for symlinks.
            current = source
            while current != state and current != current.parent:
                if current.is_symlink():
                    _fail(f"Service {name} may not follow symlinked bind paths")
                current = current.parent
            if resolved.is_file() and mount.get("read_only") is not True:
                _fail(f"Service {name} must mount state files read-only")
            target = mount["target"]
            if not target.startswith("/") or target in targets:
                _fail(f"Service {name} has invalid or duplicate mount targets")
            targets.add(target)
        if service.get("env_file"):
            _fail(f"Service {name} must not use arbitrary host env_file mounts")
        if service.get("tmpfs"):
            for mount in service["tmpfs"]:
                path = mount.get("target") if isinstance(mount, dict) else str(mount).split(":")[0]
                if path != "/tmp":
                    _fail(f"Service {name} may use tmpfs only at /tmp")
