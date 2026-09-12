import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "fastlane.py"
SPEC = importlib.util.spec_from_file_location("fastlane", MODULE_PATH)
fastlane = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fastlane)


MANIFEST = """\
schema_version = 1
profile = "task-local-fast-lane"
task_id = "TEST"
task_file = "TASK.md"
allowed_reads = ["TASK.md", "src/work.py", "tests/test_work.py"]
runtime_reads = []
baseline_reads = ["README.md"]
allowed_writes = ["src/work.py"]
prohibited_reads = [".env"]
verification = ["python3", "-m", "unittest", "-v"]
definition_of_done = ["tests pass"]
disposable_fixture = true
contains_sensitive_data = false
requires_network = false
requires_external_context = false
requires_account_data = false
requires_secrets = false
changes_deployment = false
changes_runtime = false
changes_permissions = false
changes_public_state = false
changes_canonical_authority = false
"""


class FastLaneTests(unittest.TestCase):
    def make_fixture(self, parent: Path) -> Path:
        fixture = parent / "fixture"
        (fixture / "src").mkdir(parents=True)
        (fixture / "tests").mkdir()
        (fixture / "TASK.md").write_text("Change VALUE to 2.\n", encoding="utf-8")
        (fixture / "README.md").write_text("Safe baseline context.\n", encoding="utf-8")
        (fixture / "src/work.py").write_text("VALUE = 1\n", encoding="utf-8")
        (fixture / "tests/test_work.py").write_text("# test fixture\n", encoding="utf-8")
        (fixture / "FAST_LANE.toml").write_text(MANIFEST, encoding="utf-8")
        return fixture

    def test_valid_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.make_fixture(Path(raw))
            resolved, manifest = fastlane._validate_fixture(str(fixture))
            self.assertEqual(resolved, fixture.resolve())
            self.assertEqual(manifest["task_id"], "TEST")

    def test_sensitive_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.make_fixture(Path(raw))
            path = fixture / "FAST_LANE.toml"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "contains_sensitive_data = false", "contains_sensitive_data = true"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(fastlane.FastLaneError, "contains_sensitive_data"):
                fastlane._validate_fixture(str(fixture))

    def test_parent_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self.make_fixture(Path(raw))
            path = fixture / "FAST_LANE.toml"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'baseline_reads = ["README.md"]', 'baseline_reads = ["../outside.md"]'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(fastlane.FastLaneError, "unsafe path"):
                fastlane._validate_fixture(str(fixture))

    def test_symlinked_declared_read_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            outside = root / "outside.md"
            outside.write_text("outside\n", encoding="utf-8")
            (fixture / "README.md").unlink()
            (fixture / "README.md").symlink_to(outside)
            with self.assertRaisesRegex(fastlane.FastLaneError, "symlink"):
                fastlane._validate_fixture(str(fixture))

    def test_attestation_must_be_exact(self):
        args = argparse.Namespace(
            semantic_self_containment="PASS",
            packet_complete="YES",
            external_dependency="UNKNOWN",
        )
        with self.assertRaisesRegex(fastlane.FastLaneError, "semantic gate closed"):
            fastlane._attestation(args)

    def test_comparison_outcomes(self):
        ordinary = {"status": "PASS", "usage": {"input_tokens": 100}}
        faster = {"status": "PASS", "usage": {"input_tokens": 80}}
        slower = {"status": "PASS", "usage": {"input_tokens": 120}}
        self.assertEqual(fastlane._comparison_outcome(ordinary, faster, True), "HELPED")
        self.assertEqual(fastlane._comparison_outcome(ordinary, slower, True), "NO_CLEAR_GAIN")
        self.assertEqual(fastlane._comparison_outcome(ordinary, faster, False), "INVALID_COMPARISON")

    def test_usage_comparison_keeps_cost_components_separate(self):
        result = fastlane._usage_comparison(
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20},
            {"input_tokens": 70, "cached_input_tokens": 40, "output_tokens": 30},
        )
        self.assertEqual(result["input_tokens"]["difference"], -30)
        self.assertEqual(result["cached_input_tokens"]["change_percent"], -50.0)
        self.assertEqual(result["output_tokens"]["change_percent"], 50.0)

    def test_per_fixture_lock_rejects_second_runner(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root).resolve()
            state = root / "state"
            with mock.patch.dict(os.environ, {"CODEX_FAST_LANE_HOME": str(state)}):
                with fastlane._fixture_lock(fixture):
                    with self.assertRaisesRegex(fastlane.FastLaneError, "active or stale lock"):
                        with fastlane._fixture_lock(fixture):
                            pass

    def test_legacy_sandbox_config_is_detected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                'sandbox_mode = "danger-full-access"\n', encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                hits = fastlane._legacy_config_hits()
            self.assertEqual(len(hits), 1)
            self.assertIn("sandbox_mode", hits[0])

    def test_hosted_web_search_must_be_explicitly_disabled(self):
        with tempfile.TemporaryDirectory() as raw:
            codex_home = Path(raw)
            profile = codex_home / f"{fastlane.PROFILE_ID}.config.toml"
            profile.write_text('web_search = "cached"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                with self.assertRaisesRegex(fastlane.FastLaneError, "hosted web search"):
                    fastlane._require_hosted_web_search_disabled()
                profile.write_text('web_search = "disabled"\n', encoding="utf-8")
                self.assertEqual(fastlane._require_hosted_web_search_disabled(), "disabled")

    def test_model_command_disables_hosted_web_search(self):
        with mock.patch("shutil.which", return_value="/usr/bin/codex"):
            command = fastlane._common_model_args(
                "gpt-example", "low", Path("/tmp/fixture"), Path("/tmp/final.txt")
            )
        self.assertIn('web_search="disabled"', command)

    def test_installed_wheel_profile_location_is_supported(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data_root = root / "installed-data"
            profile = (
                data_root
                / "share"
                / "codex-task-local-fast-lane"
                / "profiles"
                / f"{fastlane.PROFILE_ID}.config.toml"
            )
            profile.parent.mkdir(parents=True)
            profile.write_text('web_search = "disabled"\n', encoding="utf-8")
            with (
                mock.patch.object(fastlane, "_package_root", return_value=root / "empty"),
                mock.patch.object(fastlane.sysconfig, "get_path", return_value=str(data_root)),
            ):
                self.assertEqual(fastlane._shipped_profile_path(), profile)

    def test_probe_python_candidates_fall_back_beyond_virtualenv_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            venv_bin = root / "venv" / "bin"
            toolchain_bin = root / "toolchain" / "bin"
            venv_bin.mkdir(parents=True)
            toolchain_bin.mkdir(parents=True)
            venv_python = venv_bin / "python3"
            toolchain_python = toolchain_bin / "python3"
            for path in (venv_python, toolchain_python):
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            with (
                mock.patch.object(fastlane.sys, "executable", str(venv_python)),
                mock.patch.dict(
                    os.environ,
                    {"PATH": os.pathsep.join((str(venv_bin), str(toolchain_bin)))},
                ),
            ):
                candidates = fastlane._sandbox_probe_python_candidates()
            self.assertEqual(
                candidates,
                [str(venv_python.resolve()), str(toolchain_python.resolve())],
            )

    def test_uninstall_preserves_unowned_codex_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex"
            state_home = root / "state"
            codex_home.mkdir()
            base_config = codex_home / "config.toml"
            auth = codex_home / "auth.json"
            base_config.write_text("model = 'example'\n", encoding="utf-8")
            auth.write_text("{}\n", encoding="utf-8")
            profile = codex_home / f"{fastlane.PROFILE_ID}.config.toml"
            profile.write_bytes(
                (fastlane._package_root() / "profiles" / profile.name).read_bytes()
            )
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home), "CODEX_FAST_LANE_HOME": str(state_home)},
            ):
                fastlane._ensure_state_root()
                runs = state_home / "runs"
                runs.mkdir()
                fastlane._create_owned_run(runs / "run-1", "test")
                with mock.patch("builtins.print"):
                    self.assertEqual(fastlane._uninstall(argparse.Namespace()), 0)
            self.assertFalse(profile.exists())
            self.assertFalse(state_home.exists())
            self.assertTrue(base_config.exists())
            self.assertTrue(auth.exists())

    @unittest.skipUnless(os.name == "posix", "process-group cleanup test requires POSIX")
    def test_timeout_terminates_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "descendant-survived.txt"
            descendant = (
                "import pathlib,signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                "time.sleep(30)"
            )
            completed, _ = fastlane._run_command(
                [sys.executable, "-c", parent], root, timeout=0.1
            )
            time.sleep(1.0)
            self.assertEqual(completed.returncode, 124)
            self.assertTrue(completed.timed_out)
            self.assertEqual(completed.termination["strategy"], "posix-process-group")
            self.assertTrue(completed.termination["kill_sent"])
            self.assertTrue(
                completed.termination["original_process_group_cleanup_confirmed"]
            )
            self.assertFalse(completed.termination["descendant_containment_proven"])
            self.assertFalse(completed.termination["cleanup_confirmed"])
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "setsid regression requires POSIX")
    def test_timeout_does_not_claim_cleanup_for_detached_descendant(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "detached-descendant-survived.txt"
            descendant = (
                "import os,pathlib,signal,time; "
                "os.setsid(); "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                "time.sleep(30)"
            )
            completed, _ = fastlane._run_command(
                [sys.executable, "-c", parent], root, timeout=0.1
            )
            time.sleep(1.0)
            self.assertEqual(completed.returncode, 124)
            self.assertTrue(completed.timed_out)
            self.assertFalse(completed.termination["descendant_containment_proven"])
            self.assertFalse(completed.termination["cleanup_confirmed"])
            self.assertTrue(marker.exists())

    def test_transactional_copyback_rolls_back_first_file_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            (fixture / "src/other.py").write_text("OTHER = 1\n", encoding="utf-8")
            manifest_path = fixture / "FAST_LANE.toml"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                .replace(
                    'allowed_reads = ["TASK.md", "src/work.py", "tests/test_work.py"]',
                    'allowed_reads = ["TASK.md", "src/work.py", "src/other.py", "tests/test_work.py"]',
                )
                .replace(
                    'allowed_writes = ["src/work.py"]',
                    'allowed_writes = ["src/work.py", "src/other.py"]',
                ),
                encoding="utf-8",
            )
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            (stage / "src/other.py").write_text("OTHER = 2\n", encoding="utf-8")
            run_root = root / "run"
            fastlane._create_owned_run(run_root, "fast-run")

            real_replace_at = fastlane._replace_at
            failed = False

            def fail_second_output_once(directory_descriptor, source_name, destination_name):
                nonlocal failed
                if (
                    not failed
                    and destination_name == "other.py"
                    and ".fast-lane-" in source_name
                    and source_name.endswith(".tmp")
                ):
                    failed = True
                    raise OSError("synthetic second-output failure")
                return real_replace_at(directory_descriptor, source_name, destination_name)

            with mock.patch.object(
                fastlane, "_replace_at", side_effect=fail_second_output_once
            ):
                result = fastlane._transactional_copyback(
                    stage=stage,
                    fixture=fixture,
                    manifest=manifest,
                    preimages=preimages,
                    run_root=run_root,
                )

            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertEqual(result["replaced_files"], ["src/work.py"])
            self.assertEqual(result["restored_files"], ["src/work.py"])
            self.assertEqual((fixture / "src/work.py").read_text(), "VALUE = 1\n")
            self.assertEqual((fixture / "src/other.py").read_text(), "OTHER = 1\n")
            journal = json.loads((run_root / "copyback-transaction.json").read_text())
            self.assertEqual(journal["status"], "ROLLED_BACK")
            fastlane._validate_copyback_journal(
                journal, run_root / "copyback-transaction.json"
            )
            self.assertEqual(
                [item["path"] for item in journal["artifacts"]],
                ["src/work.py", "src/other.py"],
            )
            self.assertTrue(all(item["temporary"] for item in journal["artifacts"]))

    def test_failed_rollback_preserves_backup_for_manual_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            (fixture / "src/other.py").write_text("OTHER = 1\n", encoding="utf-8")
            manifest_path = fixture / "FAST_LANE.toml"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                .replace(
                    'allowed_reads = ["TASK.md", "src/work.py", "tests/test_work.py"]',
                    'allowed_reads = ["TASK.md", "src/work.py", "src/other.py", "tests/test_work.py"]',
                )
                .replace(
                    'allowed_writes = ["src/work.py"]',
                    'allowed_writes = ["src/work.py", "src/other.py"]',
                ),
                encoding="utf-8",
            )
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            (stage / "src/other.py").write_text("OTHER = 2\n", encoding="utf-8")
            run_root = root / "run"
            run_root.mkdir()

            real_replace_at = fastlane._replace_at
            commit_failed = False

            def fail_commit_and_rollback(directory_descriptor, source_name, destination_name):
                nonlocal commit_failed
                if (
                    not commit_failed
                    and destination_name == "other.py"
                    and source_name.endswith(".tmp")
                ):
                    commit_failed = True
                    raise OSError("synthetic commit failure")
                if (
                    commit_failed
                    and destination_name == "work.py"
                    and source_name.endswith(".bak")
                ):
                    raise OSError("synthetic rollback failure")
                return real_replace_at(directory_descriptor, source_name, destination_name)

            with mock.patch.object(
                fastlane, "_replace_at", side_effect=fail_commit_and_rollback
            ):
                result = fastlane._transactional_copyback(
                    stage=stage,
                    fixture=fixture,
                    manifest=manifest,
                    preimages=preimages,
                    run_root=run_root,
                )

            self.assertEqual(result["status"], "ROLLBACK_FAILED")
            self.assertEqual((fixture / "src/work.py").read_text(), "VALUE = 2\n")
            self.assertEqual(len(result["recovery_artifacts"]), 1)
            backup = Path(result["recovery_artifacts"][0])
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(), "VALUE = 1\n")

    @unittest.skipUnless(os.name == "posix", "SIGKILL restart fence test requires POSIX")
    def test_restart_blocks_after_launcher_dies_mid_copyback(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            (fixture / "src/other.py").write_text("OTHER = 1\n", encoding="utf-8")
            manifest_path = fixture / "FAST_LANE.toml"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                .replace(
                    'allowed_reads = ["TASK.md", "src/work.py", "tests/test_work.py"]',
                    'allowed_reads = ["TASK.md", "src/work.py", "src/other.py", "tests/test_work.py"]',
                )
                .replace(
                    'allowed_writes = ["src/work.py"]',
                    'allowed_writes = ["src/work.py", "src/other.py"]',
                ),
                encoding="utf-8",
            )
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            (stage / "src/other.py").write_text("OTHER = 2\n", encoding="utf-8")
            state_home = root / "state"

            with mock.patch.dict(
                os.environ, {"CODEX_FAST_LANE_HOME": str(state_home)}, clear=False
            ):
                fastlane._ensure_state_root()
                runs = state_home / "runs"
                runs.mkdir()
                run_root = runs / "crashed-run"
                fastlane._create_owned_run(run_root, "fast-run")
                crash_script = f"""
import os
from pathlib import Path
import signal
import fastlane

fixture = Path({str(fixture)!r}).resolve()
stage = Path({str(stage)!r}).resolve()
run_root = Path({str(run_root)!r}).resolve()
manifest = fastlane._load_manifest(fixture)
preimages = fastlane._source_preimages(fixture, manifest)
real_replace_at = fastlane._replace_at

def crash_after_first_commit(directory_descriptor, source_name, destination_name):
    real_replace_at(directory_descriptor, source_name, destination_name)
    if destination_name == "work.py" and source_name.endswith(".tmp"):
        os.kill(os.getpid(), signal.SIGKILL)

fastlane._replace_at = crash_after_first_commit
fastlane._transactional_copyback(
    stage=stage,
    fixture=fixture,
    manifest=manifest,
    preimages=preimages,
    run_root=run_root,
)
"""
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(MODULE_PATH.parent)
                crashed = subprocess.run(
                    [sys.executable, "-c", crash_script],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
                self.assertEqual((fixture / "src/work.py").read_text(), "VALUE = 2\n")
                self.assertEqual((fixture / "src/other.py").read_text(), "OTHER = 1\n")
                journal = json.loads(
                    (run_root / "copyback-transaction.json").read_text(encoding="utf-8")
                )
                self.assertEqual(journal["status"], "COMMITTING")
                self.assertEqual(journal["fixture"], str(fixture))
                with fastlane._fixture_lock(fixture):
                    with self.assertRaisesRegex(fastlane.FastLaneError, "RECOVERY_REQUIRED"):
                        fastlane._require_no_unfinished_copyback(fixture)
                journal["status"] = "ROLLED_BACK"
                journal["restored_files"] = list(journal["replaced_files"])
                journal["terminal_source_hashes"] = dict(journal["source_preimages"])
                journal["terminal_recorded_at_utc"] = "2026-09-11T00:00:00+00:00"
                fastlane._write_copyback_journal(run_root, journal)
                forged = json.loads(
                    (run_root / "copyback-transaction.json").read_text(encoding="utf-8")
                )
                fastlane._validate_copyback_journal(
                    forged,
                    run_root / "copyback-transaction.json",
                    require_terminal_seal=False,
                )
                with self.assertRaisesRegex(
                    fastlane.FastLaneError, "live fixture bytes"
                ):
                    fastlane._seal_terminal_copyback(run_root)
                with fastlane._fixture_lock(fixture):
                    with self.assertRaisesRegex(fastlane.FastLaneError, "RECOVERY_REQUIRED"):
                        fastlane._require_no_unfinished_copyback(fixture)

    @unittest.skipUnless(os.name == "posix", "directory identity test requires POSIX")
    def test_parent_directory_symlink_swap_never_writes_outside(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            path_identity = fastlane._capture_copyback_path_identity(fixture, manifest)
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            run_root = root / "run"
            fastlane._create_owned_run(run_root, "fast-run")
            detached = root / "detached-src"
            outside = root / "outside"
            outside.mkdir()
            outside_output = outside / "work.py"
            outside_output.write_text("OUTSIDE = 1\n", encoding="utf-8")

            real_replace_at = fastlane._replace_at
            swapped = False

            def swap_parent_then_replace(directory_descriptor, source_name, destination_name):
                nonlocal swapped
                if not swapped and destination_name == "work.py" and source_name.endswith(".tmp"):
                    swapped = True
                    (fixture / "src").rename(detached)
                    (fixture / "src").symlink_to(outside, target_is_directory=True)
                return real_replace_at(directory_descriptor, source_name, destination_name)

            with mock.patch.object(
                fastlane, "_replace_at", side_effect=swap_parent_then_replace
            ):
                result = fastlane._transactional_copyback(
                    stage=stage,
                    fixture=fixture,
                    manifest=manifest,
                    preimages=preimages,
                    run_root=run_root,
                    path_identity=path_identity,
                )

            self.assertEqual(result["status"], "RECOVERY_REQUIRED")
            self.assertEqual(outside_output.read_text(encoding="utf-8"), "OUTSIDE = 1\n")
            self.assertEqual((detached / "work.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertTrue(any("PATH_IDENTITY" in item for item in result["cleanup_errors"]))

    def test_install_profile_force_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            outside = root / "outside-profile.toml"
            outside.write_text("outside = true\n", encoding="utf-8")
            destination = codex_home / f"{fastlane.PROFILE_ID}.config.toml"
            destination.symlink_to(outside)

            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False),
                mock.patch.object(fastlane, "_require_supported_codex", return_value=(1, 2, 3)),
            ):
                with self.assertRaisesRegex(fastlane.FastLaneError, "not a regular file"):
                    fastlane._install_profile(argparse.Namespace(force=True))

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside = true\n")

    def test_copyback_preserves_existing_executable_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            output = fixture / "src/work.py"
            output.chmod(0o755)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            run_root = root / "run"
            fastlane._create_owned_run(run_root, "fast-run")

            result = fastlane._transactional_copyback(
                stage=stage,
                fixture=fixture,
                manifest=manifest,
                preimages=preimages,
                run_root=run_root,
            )

            self.assertEqual(result["status"], "COMMITTED")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
            self.assertEqual(output.read_text(), "VALUE = 2\n")
            journal_path = run_root / "copyback-transaction.json"
            fastlane._validate_copyback_journal(
                json.loads(journal_path.read_text(encoding="utf-8")), journal_path
            )

    def test_durable_atomic_write_syncs_file_before_rename_and_directory_after(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "journal.json"
            target.write_text("old\n", encoding="utf-8")
            events = []
            real_fsync = os.fsync
            real_replace_at = fastlane._replace_at

            def record_fsync(descriptor):
                mode = os.fstat(descriptor).st_mode
                events.append("fsync-directory" if stat.S_ISDIR(mode) else "fsync-file")
                return real_fsync(descriptor)

            def record_replace(directory_descriptor, source_name, destination_name):
                events.append("replace")
                return real_replace_at(directory_descriptor, source_name, destination_name)

            with (
                mock.patch.object(fastlane.os, "fsync", side_effect=record_fsync),
                mock.patch.object(fastlane, "_replace_at", side_effect=record_replace),
            ):
                fastlane._durable_atomic_write(target, b"new\n")

            self.assertEqual(events, ["fsync-file", "replace", "fsync-directory"])
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_owned_run_durably_publishes_new_ancestor_chain_and_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "state" / "runs" / "run-1"
            synced_directories = []
            real_sync_directory = fastlane._fsync_directory_descriptor

            def record_sync_directory(directory_descriptor):
                value = os.fstat(directory_descriptor)
                synced_directories.append((value.st_dev, value.st_ino))
                return real_sync_directory(directory_descriptor)

            with mock.patch.object(
                fastlane,
                "_fsync_directory_descriptor",
                side_effect=record_sync_directory,
            ):
                fastlane._create_owned_run(run_root, "fast-run")

            for path in (run_root.parent.parent, run_root.parent, run_root):
                value = path.stat()
                self.assertIn((value.st_dev, value.st_ino), synced_directories)
            marker = run_root / fastlane.RUN_MARKER
            self.assertTrue(marker.is_file())
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["kind"], "fast-run"
            )

    def test_copyback_syncs_replacement_before_advancing_replaced_journal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            run_root = root / "run"
            fastlane._create_owned_run(run_root, "fast-run")
            fixture_parent_inode = (fixture / "src").stat().st_ino
            events = []
            real_replace_at = fastlane._replace_at
            real_sync_directory = fastlane._fsync_directory_descriptor
            real_write_journal = fastlane._write_copyback_journal

            def record_replace(directory_descriptor, source_name, destination_name):
                if destination_name == "work.py" and source_name.endswith(".tmp"):
                    events.append("fixture-replace")
                return real_replace_at(directory_descriptor, source_name, destination_name)

            def record_sync_directory(directory_descriptor):
                if os.fstat(directory_descriptor).st_ino == fixture_parent_inode:
                    events.append("fixture-directory-fsync")
                return real_sync_directory(directory_descriptor)

            def record_journal(run_path, payload):
                if payload["status"] == "COMMITTING":
                    events.append(f"journal-replaced-{len(payload['replaced_files'])}")
                return real_write_journal(run_path, payload)

            with (
                mock.patch.object(fastlane, "_replace_at", side_effect=record_replace),
                mock.patch.object(
                    fastlane,
                    "_fsync_directory_descriptor",
                    side_effect=record_sync_directory,
                ),
                mock.patch.object(
                    fastlane, "_write_copyback_journal", side_effect=record_journal
                ),
            ):
                result = fastlane._transactional_copyback(
                    stage=stage,
                    fixture=fixture,
                    manifest=manifest,
                    preimages=preimages,
                    run_root=run_root,
                )

            self.assertEqual(result["status"], "COMMITTED")
            start = events.index("journal-replaced-0")
            replacement = events.index("fixture-replace", start)
            durable = events.index("fixture-directory-fsync", replacement)
            advanced = events.index("journal-replaced-1", durable)
            self.assertLess(start, replacement)
            self.assertLess(replacement, durable)
            self.assertLess(durable, advanced)

    def test_prepared_and_backup_file_contents_sync_before_prepared_journal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            run_root = root / "run"
            fastlane._create_owned_run(run_root, "fast-run")
            fixture_parent_inode = (fixture / "src").stat().st_ino
            events = []
            active_copy = None
            real_fsync = os.fsync
            real_copy = fastlane._copy_regular_file_to_new_at
            real_sync_directory = fastlane._fsync_directory_descriptor
            real_write_journal = fastlane._write_copyback_journal

            def record_fsync(descriptor):
                value = os.fstat(descriptor)
                if active_copy is not None and stat.S_ISREG(value.st_mode):
                    events.append(f"{active_copy}-file-fsync")
                return real_fsync(descriptor)

            def record_copy(source, target_directory_descriptor, target_name, mode):
                nonlocal active_copy
                active_copy = "backup" if isinstance(source, tuple) else "prepared"
                try:
                    return real_copy(
                        source, target_directory_descriptor, target_name, mode
                    )
                finally:
                    active_copy = None

            def record_sync_directory(directory_descriptor):
                if os.fstat(directory_descriptor).st_ino == fixture_parent_inode:
                    events.append("fixture-directory-fsync")
                return real_sync_directory(directory_descriptor)

            def record_journal(run_path, payload):
                if payload["status"] == "PREPARING":
                    events.append(f"journal-prepared-{len(payload['prepared_files'])}")
                return real_write_journal(run_path, payload)

            with (
                mock.patch.object(fastlane.os, "fsync", side_effect=record_fsync),
                mock.patch.object(
                    fastlane, "_copy_regular_file_to_new_at", side_effect=record_copy
                ),
                mock.patch.object(
                    fastlane,
                    "_fsync_directory_descriptor",
                    side_effect=record_sync_directory,
                ),
                mock.patch.object(
                    fastlane, "_write_copyback_journal", side_effect=record_journal
                ),
            ):
                result = fastlane._transactional_copyback(
                    stage=stage,
                    fixture=fixture,
                    manifest=manifest,
                    preimages=preimages,
                    run_root=run_root,
                )

            self.assertEqual(result["status"], "COMMITTED")
            prepared_sync = events.index("prepared-file-fsync")
            backup_sync = events.index("backup-file-fsync", prepared_sync)
            directory_sync = events.index("fixture-directory-fsync", backup_sync)
            journal_advance = events.index("journal-prepared-1", directory_sync)
            self.assertLess(prepared_sync, backup_sync)
            self.assertLess(backup_sync, directory_sync)
            self.assertLess(directory_sync, journal_advance)

    def test_directory_sync_failure_after_replace_rolls_back(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            preimages = fastlane._source_preimages(fixture, manifest)
            stage = root / "stage"
            fastlane._make_fast_stage(fixture, stage, manifest)
            (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
            run_root = root / "run"
            fastlane._create_owned_run(run_root, "fast-run")
            real_sync_directory = fastlane._fsync_directory_descriptor
            replacement_seen = False
            failed = False
            real_replace_at = fastlane._replace_at

            def notice_replace(directory_descriptor, source_name, destination_name):
                nonlocal replacement_seen
                result = real_replace_at(directory_descriptor, source_name, destination_name)
                if destination_name == "work.py" and source_name.endswith(".tmp"):
                    replacement_seen = True
                return result

            def fail_first_post_replace_sync(directory_descriptor):
                nonlocal failed
                if replacement_seen and not failed:
                    failed = True
                    raise OSError("synthetic directory fsync failure")
                return real_sync_directory(directory_descriptor)

            with (
                mock.patch.object(fastlane, "_replace_at", side_effect=notice_replace),
                mock.patch.object(
                    fastlane,
                    "_fsync_directory_descriptor",
                    side_effect=fail_first_post_replace_sync,
                ),
            ):
                result = fastlane._transactional_copyback(
                    stage=stage,
                    fixture=fixture,
                    manifest=manifest,
                    preimages=preimages,
                    run_root=run_root,
                )

            self.assertTrue(failed)
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertEqual(result["replaced_files"], ["src/work.py"])
            self.assertEqual(result["restored_files"], ["src/work.py"])
            self.assertEqual(
                (fixture / "src/work.py").read_text(encoding="utf-8"), "VALUE = 1\n"
            )

    def test_fast_run_copies_back_only_allowlisted_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            run_root = root / "run"

            def fake_model_arm(**kwargs):
                stage = kwargs["stage"]
                (stage / "src/work.py").write_text("VALUE = 2\n", encoding="utf-8")
                return {
                    "codex_exit_code": 0,
                    "elapsed_seconds": 1.0,
                    "usage": {"input_tokens": 10},
                    "tool_calls": 1,
                    "thread_id": "test",
                    "runtime_permission_evidence": {
                        "active_permission_profile": {"id": fastlane.PROFILE_ID},
                        "permission_profile_type": "managed",
                        "sandbox_policy_type": "workspace-write",
                        "cwd": str(stage),
                    },
                    "final": str(run_root / "final.txt"),
                    "events": str(run_root / "events.jsonl"),
                    "stderr": str(run_root / "stderr.txt"),
                }

            def fake_verification(*args, **kwargs):
                return {"exit_code": 0, "elapsed_seconds": 0.1, "stdout": "out", "stderr": "err"}

            attestation = {
                "FAST_LANE_SEMANTIC_SELF_CONTAINMENT": "PASS",
                "TASK_PACKET_CONTAINS_ALL_DECISION_RELEVANT_INVARIANTS": "YES",
                "DURABLE_OR_EXTERNAL_CONTEXT_DEPENDENCY": "NONE",
            }
            with (
                mock.patch.object(fastlane, "_run_model_arm", side_effect=fake_model_arm),
                mock.patch.object(fastlane, "_run_verification", side_effect=fake_verification),
            ):
                receipt = fastlane._run_fast_locked(
                    fixture,
                    manifest,
                    attestation,
                    "gpt-5.6-sol",
                    "medium",
                    10,
                    run_root,
                    copyback=True,
                )

            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["changed_files"], ["src/work.py"])
            self.assertEqual((fixture / "src/work.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            saved = json.loads((run_root / "receipt.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["copyback_performed"])

    def test_symlinked_staged_output_cannot_pass_or_copy_back(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            fixture, manifest = fastlane._validate_fixture(str(fixture))
            run_root = root / "run"
            outside = root / "outside-sentinel.txt"
            outside.write_text("SYNTHETIC_SECRET_SENTINEL\n", encoding="utf-8")

            def fake_model_arm(**kwargs):
                output = kwargs["stage"] / "src/work.py"
                output.unlink()
                output.symlink_to(outside)
                return {
                    "codex_exit_code": 0,
                    "elapsed_seconds": 1.0,
                    "usage": {"input_tokens": 10},
                    "tool_calls": 1,
                    "thread_id": "test",
                    "runtime_permission_evidence": {
                        "active_permission_profile": {"id": fastlane.PROFILE_ID},
                        "permission_profile_type": "managed",
                        "sandbox_policy_type": "workspace-write",
                        "cwd": str(kwargs["stage"]),
                    },
                    "final": str(run_root / "final.txt"),
                    "events": str(run_root / "events.jsonl"),
                    "stderr": str(run_root / "stderr.txt"),
                }

            def fake_verification(*args, **kwargs):
                return {"exit_code": 0, "elapsed_seconds": 0.1, "stdout": "out", "stderr": "err"}

            attestation = {
                "FAST_LANE_SEMANTIC_SELF_CONTAINMENT": "PASS",
                "TASK_PACKET_CONTAINS_ALL_DECISION_RELEVANT_INVARIANTS": "YES",
                "DURABLE_OR_EXTERNAL_CONTEXT_DEPENDENCY": "NONE",
            }
            with (
                mock.patch.object(fastlane, "_run_model_arm", side_effect=fake_model_arm),
                mock.patch.object(fastlane, "_run_verification", side_effect=fake_verification),
            ):
                receipt = fastlane._run_fast_locked(
                    fixture,
                    manifest,
                    attestation,
                    "gpt-example",
                    "low",
                    10,
                    run_root,
                    copyback=True,
                )

            self.assertEqual(receipt["status"], "FAIL")
            self.assertFalse(receipt["copyback_performed"])
            self.assertTrue(any("symlink" in blocker for blocker in receipt["blockers"]))
            self.assertEqual((fixture / "src/work.py").read_text(encoding="utf-8"), "VALUE = 1\n")


if __name__ == "__main__":
    unittest.main()
