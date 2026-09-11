import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
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

    def test_fast_run_copies_back_only_allowlisted_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = self.make_fixture(root)
            _, manifest = fastlane._validate_fixture(str(fixture))
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


if __name__ == "__main__":
    unittest.main()
