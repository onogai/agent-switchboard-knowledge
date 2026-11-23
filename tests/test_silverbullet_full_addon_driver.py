"""No-Docker safety tests for the full two-service SilverBullet E2E driver."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import silverbullet_full_addon_compose_e2e as e2e


class FullAddonE2EDriverTests(unittest.TestCase):
    def test_refuses_to_run_without_disposable_test_confirmation(self):
        with patch.object(e2e, "docker") as docker, self.assertRaises(SystemExit) as error:
            e2e.main([])
        self.assertEqual(error.exception.code, 2)
        docker.assert_not_called()

    def test_generated_project_collision_is_rejected_without_mutations(self):
        with patch.object(e2e, "docker", return_value="preexisting-project-container") as docker:
            with self.assertRaisesRegex(e2e.AcceptanceError, "already exists"):
                e2e._assert_unused_project("sbe2e-known", "sb-e2e-known")
        self.assertEqual(docker.call_count, 2)
        self.assertTrue(all(call.args[0:2] != ("network", "create")
                            for call in docker.call_args_list))

    def test_preexisting_unrelated_containers_are_not_touched(self):
        observed = []
        def docker(*args, **kwargs):
            observed.append(args)
            return ""
        with patch.object(e2e, "docker", side_effect=docker):
            e2e._assert_unused_project("sbe2e-new", "sb-e2e-new")
        self.assertTrue(any(x[:2] == ("ps", "-aq") for x in observed))
        self.assertTrue(all(x[:2] != ("ps", "-a") for x in observed))
        self.assertTrue(any("label=com.docker.compose.project=sbe2e-new" in x
                            for x in observed))

    def test_reports_safe_diagnostic_phase_without_raw_subprocess_output(self):
        def fake_docker(*args, **kwargs):
            return ""

        def fail_config(source, env_path, project, *args):
            if args[:2] == ("config", "--format"):
                raise e2e.AcceptanceError("docker exited with status 1")
            return ""

        with patch.object(e2e, "docker", side_effect=fake_docker), \
             patch.object(e2e, "_compose", side_effect=fail_config):
            with self.assertRaisesRegex(
                e2e.AcceptanceError,
                "render Docker Compose config: docker exited with status 1",
            ):
                e2e.run()

    def test_cleanup_after_config_failure_removes_only_generated_network(self):
        commands = []

        def docker(*args, **kwargs):
            commands.append(args)
            if args[:2] == ("network", "create"):
                self.assertIn("--internal", args)
                self.assertTrue(args[-1].startswith("sb-e2e-"))
            return ""

        compose_calls = []

        def compose(source, env_path, project, *args):
            compose_calls.append((Path(source), Path(env_path), project, args))
            if args[:2] == ("config", "--format"):
                raise e2e.AcceptanceError("simulated config failure")
            return ""

        with patch.object(e2e, "docker", side_effect=docker), \
             patch.object(e2e, "_compose", side_effect=compose):
            with self.assertRaisesRegex(e2e.AcceptanceError, "simulated"):
                e2e.run()

        self.assertEqual(len([c for c in commands if c[:2] == ("network", "create")]), 1)
        self.assertEqual(len([c for c in commands if c[:2] == ("network", "rm")]), 1)
        self.assertTrue(any(c[3][:1] == ("down",) for c in compose_calls))
        for source, env_path, _, _ in compose_calls:
            self.assertIn("switchboard-silverbullet-addon-e2e-", str(source))
            self.assertNotIn("/home/onogai/", str(source))
            self.assertFalse(source.exists())
            self.assertFalse(env_path.exists())

    def test_inspect_blocks_host_ports_and_foreign_mounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            safe = {
                "HostConfig": {
                    "PortBindings": {}, "Privileged": False,
                    "CapAdd": None, "CapDrop": ["ALL"], "ReadonlyRootfs": True,
                },
                "NetworkSettings": {"Networks": {"sb-e2e-private": {}}},
                "Mounts": [{
                    "Source": str(state), "Destination": "/knowledge", "RW": False,
                }],
            }
            with patch.object(e2e, "docker", return_value=json.dumps([safe])):
                e2e._inspect("disposable-container", "sb-e2e-private", state,
                                 readonly_targets=frozenset({"/knowledge"}))
            for mutation in (
                lambda d: d["HostConfig"].update(PortBindings={"3000/tcp": [{}]}),
                lambda d: d["HostConfig"].update(Privileged=True),
                lambda d: d["NetworkSettings"]["Networks"].update(production={}),
                lambda d: d["Mounts"][0].update(Source="/etc"),
                lambda d: d["Mounts"][0].update(RW=True),
            ):
                import copy
                candidate = copy.deepcopy(safe)
                mutation(candidate)
                with patch.object(e2e, "docker", return_value=json.dumps([candidate])):
                    with self.assertRaises(e2e.AcceptanceError):
                        e2e._inspect("disposable-container", "sb-e2e-private", state,
                                 readonly_targets=frozenset({"/knowledge"}))

    def test_staging_copies_only_allowlisted_files_without_local_state(self):
        with tempfile.TemporaryDirectory() as td:
            package = Path(td) / "package"
            package.mkdir()
            for name in (".dockerignore", "Dockerfile", "requirements.txt",
                         "compose.addon.yaml", "switchboard-addon.yaml"):
                (package / name).write_text("test-input")
            mcp = package / "silverbullet_mcp"
            mcp.mkdir()
            (mcp / "__init__.py").write_text("")
            (mcp / "server.py").write_text("pass")
            (mcp / "private.env").write_text("DO_NOT_COPY")
            (package / ".venv").mkdir()
            (package / ".venv" / "secret").write_text("DO_NOT_COPY")
            (package / "state").mkdir()
            (package / "state" / "knowledge.md").write_text("DO_NOT_COPY")
            (package / ".env").write_text("DO_NOT_COPY")
            source = Path(td) / "staged"
            with patch.object(e2e, "PACKAGE", package):
                e2e._stage_package(source)
            self.assertEqual(
                {x.name for x in source.iterdir()},
                {".dockerignore", "Dockerfile", "requirements.txt",
                 "compose.addon.yaml", "switchboard-addon.yaml",
                 "silverbullet_mcp"},
            )
            self.assertEqual(
                {x.name for x in (source / "silverbullet_mcp").iterdir()},
                {"__init__.py", "server.py"},
            )

    def test_staging_rejects_symlinked_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            package = Path(td) / "package"
            package.mkdir()
            for name in (".dockerignore", "Dockerfile", "requirements.txt",
                         "compose.addon.yaml", "switchboard-addon.yaml"):
                (package / name).write_text("safe")
            (package / "silverbullet_mcp").mkdir()
            (package / "silverbullet_mcp" / "__init__.py").write_text("")
            (package / "Dockerfile").unlink()
            (package / "Dockerfile").symlink_to("requirements.txt")
            with patch.object(e2e, "PACKAGE", package):
                with self.assertRaisesRegex(e2e.AcceptanceError, "unsafe"):
                    e2e._stage_package(Path(td) / "staged")

    def test_bootstrap_failure_extracts_only_allowlisted_phase(self):
        import subprocess
        unsafe = "WARNING secret_password=DO_NOT_SHOW\\n"
        safe = "SilverBullet bootstrap phase: admin-login\\n"
        failed = "SilverBullet bootstrap failed at configure-agent-permissions: KnowledgeError\\n"
        result = subprocess.CompletedProcess(
            args=["docker", "logs"], returncode=0, stdout=unsafe + safe + failed,
            stderr="token=DO_NOT_SHOW",
        )
        with patch.object(e2e.subprocess, "run", return_value=result):
            msg = e2e._bootstrap_failure("disposable-container")
        self.assertIn("configure-agent-permissions", msg)
        self.assertNotIn("DO_NOT_SHOW", msg)

    def test_compose_failure_classification_never_echoes_daemon_stderr(self):
        import subprocess
        failed = subprocess.CompletedProcess(
            args=["docker", "compose"], returncode=1,
            stdout="INTERNAL_SECRET_DO_NOT_ECHO",
            stderr="Error: dependency failed to start; secret=INTERNAL_SECRET_DO_NOT_ECHO",
        )
        with patch.object(e2e.subprocess, "run", return_value=failed):
            with self.assertRaises(e2e.AcceptanceError) as error:
                e2e.command("docker", "compose", "up")
        self.assertIn("Compose dependency could not start", str(error.exception))
        self.assertNotIn("INTERNAL_SECRET_DO_NOT_ECHO", str(error.exception))

    def test_adapter_start_has_no_deps_after_bootstrap_verification(self):
        source = (Path(__file__).resolve().parents[1] /
                  "scripts" / "silverbullet_full_addon_compose_e2e.py").read_text()
        self.assertIn('"--no-deps", "silverbulletadapter"', source)
        self.assertIn('if init_state.get("ExitCode") != 0:', source)

    def test_command_errors_never_echo_credentials(self):
        import subprocess
        failed = subprocess.CompletedProcess(
            args=["docker"], returncode=1, stdout="TEST_SECRET", stderr="TEST_SECRET"
        )
        with patch.object(e2e.subprocess, "run", return_value=failed):
            with self.assertRaises(e2e.AcceptanceError) as error:
                e2e.command("docker", "compose", "config")
        self.assertNotIn("TEST_SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
