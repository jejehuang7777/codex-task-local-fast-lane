#!/usr/bin/env python3
"""Run and measure small, self-contained Codex tasks in an isolated fast lane.

This tool is deliberately fail closed. It copies only manifest-declared files
into a generated repository, requests a no-network permission profile, runs an
exact verifier, checks the changed-file set, and copies back only allowlisted
outputs whose source preimages have not changed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import uuid


PROFILE_ID = "task-local-fast-lane"
MANIFEST_NAME = "FAST_LANE.toml"
MIN_CODEX_VERSION = (0, 138, 0)
REQUIRED_FALSE_FLAGS = (
    "contains_sensitive_data",
    "requires_network",
    "requires_external_context",
    "requires_account_data",
    "requires_secrets",
    "changes_deployment",
    "changes_runtime",
    "changes_permissions",
    "changes_public_state",
    "changes_canonical_authority",
)
IGNORED_SCOPE_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules"}
DISABLED_FEATURES = (
    "apps",
    "plugins",
    "memories",
    "multi_agent",
    "browser_use",
    "computer_use",
    "in_app_browser",
    "workspace_dependencies",
    "hooks",
)

FAST_AGENTS = """\
# Task-local fast lane

This generated checkout is a disposable, self-contained task fixture. Read
`FAST_LANE.toml`, then its `task_file`. Read only `allowed_reads` for task
interpretation. `runtime_reads` exist only for imports and verification. Edit
only `allowed_writes`. Do not inspect parent directories, memory, plugins,
apps, MCP servers, connectors, browsers, or the network. Run the exact
verification and stop. If the packet is incomplete, stop instead of widening
scope.
"""

BASELINE_AGENTS = """\
# Safe A/B baseline fixture

This is a generated, disposable, non-sensitive fixture. Execute TASK.md, make
the smallest complete repair, run the exact verification, and do not use the
network or inspect parent directories. Do not modify files unrelated to the
task.
"""


class FastLaneError(RuntimeError):
    """A fail-closed validation or execution error."""


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def _state_root() -> Path:
    raw = os.environ.get("CODEX_FAST_LANE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex-fast-lane"


STATE_MARKER = ".codex-task-local-fast-lane-state.json"
RUN_MARKER = ".codex-task-local-fast-lane-run.json"


def _ensure_state_root() -> Path:
    root = _state_root()
    marker = root / STATE_MARKER
    if root.exists() and not marker.is_file():
        raise FastLaneError(
            f"state directory exists without this beta's ownership marker: {root}"
        )
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(
            json.dumps({"owner": "codex-task-local-fast-lane", "schema_version": 1}, indent=2) + "\n",
            encoding="utf-8",
        )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FastLaneError(f"invalid state ownership marker: {marker}") from exc
    if payload != {"owner": "codex-task-local-fast-lane", "schema_version": 1}:
        raise FastLaneError(f"state ownership marker does not match this beta: {marker}")
    return root


def _create_owned_run(path: Path, kind: str) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / RUN_MARKER).write_text(
        json.dumps(
            {"owner": "codex-task-local-fast-lane", "schema_version": 1, "kind": kind},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _validate_owned_run(path: Path) -> None:
    marker = path / RUN_MARKER
    if not path.is_dir() or not marker.is_file():
        raise FastLaneError(f"run ownership is unknown; refusing cleanup: {path}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FastLaneError(f"invalid run ownership marker: {marker}") from exc
    if payload.get("owner") != "codex-task-local-fast-lane" or payload.get("schema_version") != 1:
        raise FastLaneError(f"run ownership marker does not match this beta: {marker}")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative_path(raw: str, field: str) -> PurePosixPath:
    value = PurePosixPath(raw)
    if not raw or value.is_absolute() or ".." in value.parts or value.as_posix() == ".":
        raise FastLaneError(f"{field} contains an unsafe path: {raw!r}")
    return value


def _path_has_symlink(root: Path, rel: PurePosixPath) -> bool:
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _validate_declared_file(root: Path, rel: PurePosixPath, field: str) -> None:
    path = root / rel
    if not path.is_file():
        raise FastLaneError(f"{field} file does not exist: {rel.as_posix()}")
    if _path_has_symlink(root, rel) or not _inside(path.resolve(), root):
        raise FastLaneError(f"{field} escapes or uses a symlink: {rel.as_posix()}")


def _require_regular_contained_file(root: Path, rel: PurePosixPath, field: str) -> Path:
    """Reject output symlinks/ancestors before the privileged launcher reads them."""
    path = root / rel
    if _path_has_symlink(root, rel):
        raise FastLaneError(f"{field} uses a symlink: {rel.as_posix()}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise FastLaneError(f"{field} is missing: {rel.as_posix()}") from exc
    if not stat.S_ISREG(mode):
        raise FastLaneError(f"{field} is not a regular file: {rel.as_posix()}")
    root_resolved = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not _inside(resolved, root_resolved):
        raise FastLaneError(f"{field} escapes staging: {rel.as_posix()}")
    return path


def _load_manifest(fixture: Path) -> dict:
    manifest_path = fixture / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise FastLaneError(f"missing regular {MANIFEST_NAME}: {manifest_path}")
    with manifest_path.open("rb") as handle:
        data = tomllib.load(handle)

    if data.get("schema_version") != 1:
        raise FastLaneError("FAST_LANE.toml schema_version must be 1")
    if data.get("profile") != PROFILE_ID:
        raise FastLaneError(f"manifest profile must be {PROFILE_ID!r}")
    if data.get("disposable_fixture") is not True:
        raise FastLaneError("disposable_fixture must be explicitly true")

    for flag in REQUIRED_FALSE_FLAGS:
        if data.get(flag) is not False:
            raise FastLaneError(f"{flag} must be explicitly false")

    task_file = _relative_path(str(data.get("task_file", "")), "task_file")
    for field in (
        "allowed_reads",
        "runtime_reads",
        "baseline_reads",
        "allowed_writes",
        "prohibited_reads",
    ):
        values = data.get(field)
        if not isinstance(values, list):
            raise FastLaneError(f"{field} must be a list")
        data[field] = [_relative_path(str(value), field) for value in values]

    if not data["allowed_reads"] or not data["allowed_writes"]:
        raise FastLaneError("allowed_reads and allowed_writes must both be non-empty")
    if task_file not in data["allowed_reads"]:
        raise FastLaneError("task_file must also appear in allowed_reads")
    data["task_file"] = task_file

    readable = set(data["allowed_reads"]) | set(data["runtime_reads"])
    all_inputs = readable | set(data["baseline_reads"])
    if len(all_inputs) != len(data["allowed_reads"] + data["runtime_reads"] + data["baseline_reads"]):
        raise FastLaneError("read lists must not contain duplicate paths")

    prohibited = set(data["prohibited_reads"])
    if prohibited & (all_inputs | set(data["allowed_writes"])):
        raise FastLaneError("a prohibited path is also declared readable or writable")

    for rel in all_inputs:
        _validate_declared_file(fixture, rel, "declared read")

    for rel in data["allowed_writes"]:
        destination = fixture / rel
        if _path_has_symlink(fixture, rel):
            raise FastLaneError(f"allowed write uses a symlink: {rel.as_posix()}")
        parent = destination.parent.resolve()
        if not _inside(parent, fixture):
            raise FastLaneError(f"allowed write escapes fixture: {rel.as_posix()}")
        if destination.exists() and rel not in readable:
            raise FastLaneError(f"existing write target must also be readable: {rel.as_posix()}")

    verification = data.get("verification")
    if (
        not isinstance(verification, list)
        or not verification
        or any(not isinstance(part, str) or not part for part in verification)
    ):
        raise FastLaneError("verification must be a non-empty argv list")

    definition = data.get("definition_of_done")
    if not isinstance(definition, list) or not definition or any(not str(item).strip() for item in definition):
        raise FastLaneError("definition_of_done must be a non-empty list")
    return data


def _validate_fixture(raw_fixture: str) -> tuple[Path, dict]:
    fixture = Path(raw_fixture).expanduser().resolve(strict=True)
    if not fixture.is_dir():
        raise FastLaneError("fixture must be a directory")
    if fixture == Path(fixture.anchor):
        raise FastLaneError("fixture cannot be a filesystem root")
    return fixture, _load_manifest(fixture)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FastLaneError(f"refusing to hash non-regular or symlinked path: {path}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED_SCOPE_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise FastLaneError(f"staging tree contains a symlink: {relative.as_posix()}")
        if not path.is_file():
            continue
        if not _inside(path.resolve(strict=True), root.resolve(strict=True)):
            raise FastLaneError(f"staging tree path escapes its root: {relative.as_posix()}")
        result[relative.as_posix()] = _hash(path)
    return result


def _validate_stage_outputs(stage: Path, manifest: dict) -> None:
    for rel in manifest["allowed_writes"]:
        _require_regular_contained_file(stage, rel, "staged output")


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _source_preimages(fixture: Path, manifest: dict) -> dict[str, str | None]:
    paths = (
        set(manifest["allowed_reads"])
        | set(manifest["runtime_reads"])
        | set(manifest["allowed_writes"])
        | {PurePosixPath(MANIFEST_NAME)}
    )
    return {
        rel.as_posix(): _hash(fixture / rel) if (fixture / rel).is_file() else None
        for rel in sorted(paths, key=lambda item: item.as_posix())
    }


def _preimage_conflicts(fixture: Path, expected: dict[str, str | None]) -> dict:
    conflicts = {}
    for raw, expected_hash in expected.items():
        rel = PurePosixPath(raw)
        path = fixture / rel
        if _path_has_symlink(fixture, rel):
            conflicts[raw] = {"expected": expected_hash, "actual": "SYMLINK"}
            continue
        actual_hash = _hash(path) if path.is_file() else None
        if actual_hash != expected_hash:
            conflicts[raw] = {"expected": expected_hash, "actual": actual_hash}
    return conflicts


@contextmanager
def _fixture_lock(fixture: Path):
    lock_root = _ensure_state_root() / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(str(fixture).encode("utf-8")).hexdigest()
    lock_path = lock_root / lock_name
    try:
        lock_path.mkdir()
    except FileExistsError as exc:
        raise FastLaneError(
            f"fixture already has an active or stale lock: {lock_path}; "
            "do not remove it until you have confirmed no run is active"
        ) from exc
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": os.getpid(), "fixture": str(fixture)}, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        yield lock_path
    finally:
        shutil.rmtree(lock_path, ignore_errors=True)


def _copy_paths(source: Path, destination: Path, paths: set[PurePosixPath]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for rel in sorted(paths, key=lambda item: item.as_posix()):
        src = source / rel
        dst = destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _init_generated_repo(stage: Path, agents_text: str, preserve_agents: bool = False) -> None:
    (stage / ".git").mkdir(exist_ok=True)
    (stage / ".git" / "HEAD").write_text("ref: refs/heads/fast-lane\n", encoding="utf-8")
    (stage / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    agents = stage / "AGENTS.md"
    if not preserve_agents or not agents.exists():
        agents.write_text(agents_text, encoding="utf-8")


def _make_fast_stage(fixture: Path, stage: Path, manifest: dict) -> None:
    paths = set(manifest["allowed_reads"]) | set(manifest["runtime_reads"])
    paths.add(PurePosixPath(MANIFEST_NAME))
    _copy_paths(fixture, stage, paths)
    _init_generated_repo(stage, FAST_AGENTS)


def _make_baseline_stage(fixture: Path, stage: Path, manifest: dict) -> None:
    paths = (
        set(manifest["allowed_reads"])
        | set(manifest["runtime_reads"])
        | set(manifest["baseline_reads"])
        | {PurePosixPath(MANIFEST_NAME)}
    )
    _copy_paths(fixture, stage, paths)
    _init_generated_repo(stage, BASELINE_AGENTS, preserve_agents=True)


def _parse_events(stdout: str) -> tuple[str | None, dict | None, int]:
    thread_id = None
    usage = None
    tool_calls = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
        item = event.get("item") or {}
        if event.get("type") == "item.started" and item.get("type") in {
            "command_execution",
            "mcp_tool_call",
            "web_search",
        }:
            tool_calls += 1
    return thread_id, usage, tool_calls


def _runtime_permission_evidence(thread_id: str | None, now: dt.datetime) -> dict | None:
    if not thread_id:
        return None
    month_root = _codex_home() / "sessions" / now.strftime("%Y") / now.strftime("%m")
    candidates = sorted(month_root.rglob(f"rollout-*-{thread_id}.jsonl")) if month_root.is_dir() else []
    for rollout in reversed(candidates):
        latest = None
        with rollout.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "turn_context":
                    latest = event.get("payload", {})
        if latest is not None:
            return {
                "active_permission_profile": latest.get("active_permission_profile"),
                "permission_profile_type": (latest.get("permission_profile") or {}).get("type"),
                "sandbox_policy_type": (latest.get("sandbox_policy") or {}).get("type"),
                "cwd": latest.get("cwd"),
            }
    return None


def _run_command(
    argv: list[str],
    cwd: Path,
    timeout: int,
    stdin: str | None = None,
    env_additions: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, float]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_additions:
        env.update(env_additions)
    started = time.monotonic()
    completed = subprocess.run(
        argv,
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return completed, round(time.monotonic() - started, 3)


def _toml_key(raw: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        return raw
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mcp_disable_overrides() -> list[str]:
    config = _codex_home() / "config.toml"
    if not config.is_file():
        return []
    try:
        with config.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    result = []
    for name in servers:
        result.extend(["-c", f"mcp_servers.{_toml_key(str(name))}.enabled=false"])
    return result


def _profile_path() -> Path:
    return _codex_home() / f"{PROFILE_ID}.config.toml"


def _require_profile() -> None:
    path = _profile_path()
    if not path.is_file():
        raise FastLaneError(
            f"permission profile is not installed at {path}; run: "
            "python3 fastlane.py install-profile"
        )
    shipped = _package_root() / "profiles" / f"{PROFILE_ID}.config.toml"
    if path.read_bytes() != shipped.read_bytes():
        raise FastLaneError(
            f"installed profile differs from this beta's reviewed profile: {path}; "
            "inspect it, then use install-profile --force if replacement is intended"
        )


def _codex_version() -> tuple[int, int, int]:
    codex = shutil.which("codex")
    if not codex:
        raise FastLaneError("codex executable not found")
    completed, _ = _run_command([codex, "--version"], _package_root(), 30)
    if completed.returncode != 0:
        raise FastLaneError(f"could not read Codex version: {completed.stderr.strip()}")
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout)
    if not match:
        raise FastLaneError(f"unrecognized Codex version: {completed.stdout.strip()}")
    return tuple(int(value) for value in match.groups())


def _require_supported_codex() -> tuple[int, int, int]:
    version = _codex_version()
    if version < MIN_CODEX_VERSION:
        required = ".".join(str(value) for value in MIN_CODEX_VERSION)
        actual = ".".join(str(value) for value in version)
        raise FastLaneError(f"Codex {required}+ is required; found {actual}")
    return version


def _legacy_config_hits() -> list[str]:
    hits = []
    for path in (_codex_home() / "config.toml", _profile_path()):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FastLaneError(f"cannot validate Codex config: {path}") from exc
        for key in ("sandbox_mode", "sandbox_workspace_write"):
            if key in data:
                hits.append(f"{path}:{key}")
    return hits


def _require_hosted_web_search_disabled() -> str:
    path = _profile_path()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FastLaneError(f"cannot validate hosted web-search setting: {path}") from exc
    if data.get("web_search") != "disabled":
        raise FastLaneError(
            f'hosted web search must be explicitly disabled in {path}: web_search = "disabled"'
        )
    return "disabled"


def _effective_boundary_preflight() -> dict:
    """Probe the effective named profile without spending a model turn."""
    hits = _legacy_config_hits()
    if hits:
        raise FastLaneError(
            "legacy sandbox settings conflict with permission-profile proof: " + ", ".join(hits)
        )
    hosted_web_search = _require_hosted_web_search_disabled()
    codex = shutil.which("codex")
    if not codex:
        raise FastLaneError("codex executable not found")

    runs_root = _ensure_state_root() / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    probe_root = runs_root / f"preflight-{uuid.uuid4().hex[:12]}"
    _create_owned_run(probe_root, "boundary-preflight")
    workspace = probe_root / "workspace"
    outside = probe_root / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "sentinel.txt"
    secret.write_text("must-not-read\n", encoding="utf-8")
    outside_write = outside / "must-not-write.txt"
    base = [
        codex,
        "sandbox",
        "-p",
        PROFILE_ID,
        "-P",
        PROFILE_ID,
        "-C",
        str(workspace),
        "--",
    ]
    probes = {}
    try:
        allowed, _ = _run_command(
            base
            + [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('allowed.txt').write_text('ok')",
            ],
            workspace,
            30,
        )
        probes["workspace_write_allowed"] = allowed.returncode == 0 and (workspace / "allowed.txt").is_file()

        denied_read, _ = _run_command(
            base
            + [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).read_text()",
                str(secret),
            ],
            workspace,
            30,
        )
        probes["outside_read_denied"] = denied_read.returncode != 0

        denied_write, _ = _run_command(
            base
            + [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('bad')",
                str(outside_write),
            ],
            workspace,
            30,
        )
        probes["outside_write_denied"] = denied_write.returncode != 0 and not outside_write.exists()

        denied_network, _ = _run_command(
            base
            + [
                sys.executable,
                "-c",
                "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'x', ('127.0.0.1', 9))",
            ],
            workspace,
            30,
        )
        probes["network_denied"] = denied_network.returncode != 0

        hidden_env, _ = _run_command(
            base
            + [
                sys.executable,
                "-c",
                "import os,sys; sys.exit(41 if os.getenv('FASTLANE_SECRET_SENTINEL') else 0)",
            ],
            workspace,
            30,
            env_additions={"FASTLANE_SECRET_SENTINEL": "must-not-inherit"},
        )
        probes["unlisted_environment_hidden"] = hidden_env.returncode == 0
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)

    if not all(probes.values()):
        failed = sorted(name for name, passed in probes.items() if not passed)
        raise FastLaneError(f"effective permission boundary probe failed: {failed}")
    return {
        "status": "EFFECTIVE_BOUNDARY_PASS",
        "profile": PROFILE_ID,
        "hosted_web_search": hosted_web_search,
        "hosted_web_search_cli_override": True,
        "legacy_config_hits": [],
        "probes": probes,
    }


def _attestation(args: argparse.Namespace) -> dict[str, str]:
    values = {
        "FAST_LANE_SEMANTIC_SELF_CONTAINMENT": args.semantic_self_containment,
        "TASK_PACKET_CONTAINS_ALL_DECISION_RELEVANT_INVARIANTS": args.packet_complete,
        "DURABLE_OR_EXTERNAL_CONTEXT_DEPENDENCY": args.external_dependency,
    }
    expected = {
        "FAST_LANE_SEMANTIC_SELF_CONTAINMENT": "PASS",
        "TASK_PACKET_CONTAINS_ALL_DECISION_RELEVANT_INVARIANTS": "YES",
        "DURABLE_OR_EXTERNAL_CONTEXT_DEPENDENCY": "NONE",
    }
    if values != expected:
        raise FastLaneError(f"semantic gate closed: expected {expected}, got {values}")
    return values


def _verification_command(codex: str, stage: Path, verification: list[str]) -> list[str]:
    executable = shutil.which(verification[0])
    if not executable:
        raise FastLaneError(f"verification executable not found: {verification[0]}")
    return [
        codex,
        "sandbox",
        "-p",
        PROFILE_ID,
        "-P",
        PROFILE_ID,
        "-C",
        str(stage),
        "--",
        executable,
        *verification[1:],
    ]


def _common_model_args(model: str, effort: str, stage: Path, final_path: Path) -> list[str]:
    codex = shutil.which("codex")
    if not codex:
        raise FastLaneError("codex executable not found")
    args = [
        codex,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(stage),
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'web_search="disabled"',
        "-o",
        str(final_path),
    ]
    for feature in DISABLED_FEATURES:
        args.extend(["--disable", feature])
    args.extend(_mcp_disable_overrides())
    return args


def _write_evidence(run_root: Path, completed: subprocess.CompletedProcess, label: str) -> None:
    (run_root / f"{label}.events.jsonl").write_text(completed.stdout, encoding="utf-8")
    (run_root / f"{label}.stderr.txt").write_text(completed.stderr, encoding="utf-8")


def _run_model_arm(
    *,
    stage: Path,
    run_root: Path,
    label: str,
    prompt: str,
    model: str,
    effort: str,
    timeout: int,
    fast: bool,
) -> dict:
    final_path = run_root / f"{label}.final.txt"
    command = _common_model_args(model, effort, stage, final_path)
    command[2:2] = ["-p", PROFILE_ID, "-c", f'default_permissions="{PROFILE_ID}"']
    command.append("-")
    now = dt.datetime.now(dt.timezone.utc)
    completed, elapsed = _run_command(command, stage, timeout, stdin=prompt)
    _write_evidence(run_root, completed, label)
    thread_id, usage, tool_calls = _parse_events(completed.stdout)
    permission = _runtime_permission_evidence(thread_id, now) if fast else None
    return {
        "codex_exit_code": completed.returncode,
        "elapsed_seconds": elapsed,
        "usage": usage,
        "tool_calls": tool_calls,
        "thread_id": thread_id,
        "runtime_permission_evidence": permission,
        "final": str(final_path),
        "events": str(run_root / f"{label}.events.jsonl"),
        "stderr": str(run_root / f"{label}.stderr.txt"),
    }


def _run_verification(stage: Path, run_root: Path, label: str, manifest: dict, timeout: int) -> dict:
    codex = shutil.which("codex")
    if not codex:
        raise FastLaneError("codex executable not found")
    command = _verification_command(codex, stage, manifest["verification"])
    completed, elapsed = _run_command(command, stage, timeout)
    (run_root / f"{label}.verification.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (run_root / f"{label}.verification.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    return {
        "exit_code": completed.returncode,
        "elapsed_seconds": elapsed,
        "stdout": str(run_root / f"{label}.verification.stdout.txt"),
        "stderr": str(run_root / f"{label}.verification.stderr.txt"),
    }


def _run_fast_locked(
    fixture: Path,
    manifest: dict,
    attestation: dict,
    model: str,
    effort: str,
    timeout: int,
    run_root: Path,
    copyback: bool,
) -> dict:
    stage = run_root / "fast-fixture"
    run_root.parent.mkdir(parents=True, exist_ok=True)
    _create_owned_run(run_root, "fast-run")
    preimages = _source_preimages(fixture, manifest)
    _make_fast_stage(fixture, stage, manifest)
    before = _tree_hashes(stage)
    prompt = "\n".join(
        [
            "Execute this approved bounded task-local fast-lane packet.",
            "The caller established semantic self-containment, packet completeness, and no external context dependency.",
            f"Read {manifest['task_file'].as_posix()} first.",
            "Read only allowed_reads for task meaning; runtime_reads are import support only.",
            "Edit only allowed_writes. Do not use web, browser, apps, plugins, MCP, connectors, memory, or parent inspection.",
            "Make the smallest complete repair, run the exact verifier, and report literal results.",
        ]
    )
    arm = _run_model_arm(
        stage=stage,
        run_root=run_root,
        label="fast",
        prompt=prompt,
        model=model,
        effort=effort,
        timeout=timeout,
        fast=True,
    )
    verification = _run_verification(stage, run_root, "fast", manifest, timeout)
    output_structure_error = None
    try:
        _validate_stage_outputs(stage, manifest)
        after = _tree_hashes(stage)
    except FastLaneError as exc:
        output_structure_error = str(exc)
        after = before
    changed = _changed_files(before, after)
    allowed = {path.as_posix() for path in manifest["allowed_writes"]}
    unexpected = sorted(set(changed) - allowed)
    conflicts = _preimage_conflicts(fixture, preimages)
    blockers = []
    permission = arm["runtime_permission_evidence"]
    if arm["codex_exit_code"] != 0:
        blockers.append(f"codex exec returned {arm['codex_exit_code']}")
    if output_structure_error:
        blockers.append(output_structure_error)
    if permission is None:
        blockers.append("active permission profile could not be read back")
    elif (permission.get("active_permission_profile") or {}).get("id") != PROFILE_ID:
        blockers.append(f"active permission profile mismatch: {permission.get('active_permission_profile')}")
    elif Path(permission.get("cwd", "")).resolve() != stage.resolve():
        blockers.append(f"runtime cwd mismatch: {permission.get('cwd')}")
    if verification["exit_code"] != 0:
        blockers.append(f"exact verification returned {verification['exit_code']}")
    if unexpected:
        blockers.append(f"unexpected changed files: {unexpected}")
    if not changed:
        blockers.append("no declared output changed")
    if conflicts:
        blockers.append(f"COPYBACK_CONFLICT: {sorted(conflicts)}")

    status = "PASS" if not blockers else "FAIL"
    if status == "PASS" and copyback:
        for rel in manifest["allowed_writes"]:
            source = _require_regular_contained_file(stage, rel, "staged output before copyback")
            destination = fixture / rel
            if _path_has_symlink(fixture, rel) or not _inside(
                destination.parent.resolve(strict=True), fixture.resolve(strict=True)
            ):
                raise FastLaneError(f"COPYBACK_CONFLICT: destination path changed: {rel.as_posix()}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + f".fast-lane-{uuid.uuid4().hex}.tmp")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(source, flags)
            except OSError as exc:
                raise FastLaneError(
                    f"staged output changed before copyback: {rel.as_posix()}"
                ) from exc
            with os.fdopen(descriptor, "rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
            os.replace(temporary, destination)

    receipt = {
        "schema_version": 1,
        "status": status,
        "profile": PROFILE_ID,
        "task_id": manifest.get("task_id"),
        "source_fixture": str(fixture),
        "staging_directory": str(stage),
        "attestation": attestation,
        "model": model,
        "reasoning_effort": effort,
        **arm,
        "verification": verification,
        "changed_files": changed,
        "unexpected_changed_files": unexpected,
        "source_preimage_conflicts": conflicts,
        "copyback_performed": status == "PASS" and copyback,
        "blockers": blockers,
    }
    (run_root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def _run_baseline(
    fixture: Path,
    manifest: dict,
    model: str,
    effort: str,
    timeout: int,
    run_root: Path,
) -> dict:
    stage = run_root / "baseline-fixture"
    run_root.parent.mkdir(parents=True, exist_ok=True)
    _create_owned_run(run_root, "ordinary-arm")
    _make_baseline_stage(fixture, stage, manifest)
    before = _tree_hashes(stage)
    prompt = "\n".join(
        [
            "Execute TASK.md in this safe disposable repository.",
            "Use the repository instructions and relevant local files.",
            "Make the smallest complete repair, run the exact verifier, inspect the final changed-file set, and report literal results.",
            "Do not use the network or modify files unrelated to the task.",
        ]
    )
    arm = _run_model_arm(
        stage=stage,
        run_root=run_root,
        label="ordinary",
        prompt=prompt,
        model=model,
        effort=effort,
        timeout=timeout,
        fast=True,
    )
    verification = _run_verification(stage, run_root, "ordinary", manifest, timeout)
    output_structure_error = None
    try:
        _validate_stage_outputs(stage, manifest)
        after = _tree_hashes(stage)
    except FastLaneError as exc:
        output_structure_error = str(exc)
        after = before
    changed = _changed_files(before, after)
    allowed = {path.as_posix() for path in manifest["allowed_writes"]}
    unexpected = sorted(set(changed) - allowed)
    blockers = []
    if arm["codex_exit_code"] != 0:
        blockers.append(f"codex exec returned {arm['codex_exit_code']}")
    if output_structure_error:
        blockers.append(output_structure_error)
    permission = arm["runtime_permission_evidence"]
    if permission is None:
        blockers.append("active permission profile could not be read back")
    elif (permission.get("active_permission_profile") or {}).get("id") != PROFILE_ID:
        blockers.append(f"active permission profile mismatch: {permission.get('active_permission_profile')}")
    elif Path(permission.get("cwd", "")).resolve() != stage.resolve():
        blockers.append(f"runtime cwd mismatch: {permission.get('cwd')}")
    if verification["exit_code"] != 0:
        blockers.append(f"exact verification returned {verification['exit_code']}")
    if unexpected:
        blockers.append(f"unexpected changed files: {unexpected}")
    if not changed:
        blockers.append("no declared output changed")
    receipt = {
        "schema_version": 1,
        "status": "PASS" if not blockers else "FAIL",
        "task_id": manifest.get("task_id"),
        "source_fixture": str(fixture),
        "staging_directory": str(stage),
        "model": model,
        "reasoning_effort": effort,
        **arm,
        "verification": verification,
        "changed_files": changed,
        "unexpected_changed_files": unexpected,
        "blockers": blockers,
    }
    (run_root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def _output_hashes(root: Path, manifest: dict) -> dict[str, str | None]:
    result = {}
    for rel in manifest["allowed_writes"]:
        path = _require_regular_contained_file(root, rel, "comparison output")
        result[rel.as_posix()] = _hash(path)
    return result


def _percent_change(fast: int | float, ordinary: int | float) -> float | None:
    if ordinary == 0:
        return None
    return round((fast - ordinary) / ordinary * 100, 2)


def _comparison_outcome(ordinary: dict, fast: dict, outputs_equal: bool) -> str:
    ordinary_input = (ordinary.get("usage") or {}).get("input_tokens")
    fast_input = (fast.get("usage") or {}).get("input_tokens")
    valid = (
        ordinary.get("status") == "PASS"
        and fast.get("status") == "PASS"
        and outputs_equal
        and isinstance(ordinary_input, int)
        and isinstance(fast_input, int)
    )
    if not valid:
        return "INVALID_COMPARISON"
    return "HELPED" if fast_input < ordinary_input else "NO_CLEAR_GAIN"


def _install_profile(args: argparse.Namespace) -> int:
    version = _require_supported_codex()
    _ensure_state_root()
    source = _package_root() / "profiles" / f"{PROFILE_ID}.config.toml"
    destination = _profile_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() == source.read_bytes():
            status = "ALREADY_INSTALLED"
        elif not args.force:
            raise FastLaneError(
                f"profile already exists with different contents: {destination}; "
                "inspect it or rerun with --force"
            )
        else:
            backup = destination.with_suffix(destination.suffix + f".bak-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(destination, backup)
            shutil.copy2(source, destination)
            status = f"UPDATED_WITH_BACKUP:{backup}"
    else:
        shutil.copy2(source, destination)
        status = "INSTALLED"
    print(
        json.dumps(
            {
                "status": status,
                "profile": str(destination),
                "codex_version": ".".join(str(value) for value in version),
            },
            indent=2,
        )
    )
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    """Remove only artifacts whose ownership can be proven."""
    source_profile = _package_root() / "profiles" / f"{PROFILE_ID}.config.toml"
    installed_profile = _profile_path()
    state = _state_root()

    if installed_profile.exists() and installed_profile.read_bytes() != source_profile.read_bytes():
        raise FastLaneError(
            f"installed profile differs from this beta; refusing to remove: {installed_profile}"
        )

    owned_runs = []
    if state.exists():
        _ensure_state_root()
        allowed_top = {STATE_MARKER, "runs", "locks"}
        unknown_top = sorted(path.name for path in state.iterdir() if path.name not in allowed_top)
        if unknown_top:
            raise FastLaneError(f"state directory contains unknown entries; refusing cleanup: {unknown_top}")
        locks = state / "locks"
        if locks.exists():
            active_or_stale = list(locks.iterdir())
            if active_or_stale:
                raise FastLaneError(
                    "lock directories remain; confirm no run is active and resolve them before cleanup: "
                    + ", ".join(str(path) for path in active_or_stale)
                )
        runs = state / "runs"
        if runs.exists():
            for path in runs.iterdir():
                _validate_owned_run(path)
                owned_runs.append(path)

    removed = []
    if installed_profile.exists():
        installed_profile.unlink()
        removed.append(str(installed_profile))
    for path in owned_runs:
        shutil.rmtree(path)
        removed.append(str(path))
    if state.exists():
        runs = state / "runs"
        locks = state / "locks"
        if runs.exists():
            runs.rmdir()
        if locks.exists():
            locks.rmdir()
        (state / STATE_MARKER).unlink()
        state.rmdir()
        removed.append(str(state))

    print(
        json.dumps(
            {
                "status": "UNINSTALLED" if removed else "NOT_INSTALLED",
                "removed": removed,
                "preserved": [
                    str(_codex_home() / "config.toml"),
                    str(_codex_home() / "auth.json"),
                    "all repositories and all other user files",
                ],
            },
            indent=2,
        )
    )
    return 0


def _preflight(args: argparse.Namespace) -> int:
    version = _require_supported_codex()
    _require_profile()
    fixture, manifest = _validate_fixture(args.fixture)
    boundary = _effective_boundary_preflight()
    result = {
        "status": "PREFLIGHT_PASS",
        "fixture": str(fixture),
        "task_id": manifest.get("task_id"),
        "attestation": _attestation(args),
        "codex_version": ".".join(str(value) for value in version),
        "profile": str(_profile_path()),
        "effective_boundary": boundary,
        "allowed_writes": [path.as_posix() for path in manifest["allowed_writes"]],
        "verification": manifest["verification"],
    }
    print(json.dumps(result, indent=2))
    return 0


def _run(args: argparse.Namespace) -> int:
    _require_supported_codex()
    _require_profile()
    fixture, manifest = _validate_fixture(args.fixture)
    attestation = _attestation(args)
    boundary = _effective_boundary_preflight()
    run_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_root = _state_root() / "runs" / run_id
    run_root.parent.mkdir(parents=True, exist_ok=True)
    with _fixture_lock(fixture):
        receipt = _run_fast_locked(
            fixture,
            manifest,
            attestation,
            args.model,
            args.effort,
            args.timeout,
            run_root,
            copyback=True,
        )
    receipt["effective_boundary"] = boundary
    (run_root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["status"] == "PASS" else 1


def _compare(args: argparse.Namespace) -> int:
    _require_supported_codex()
    _require_profile()
    fixture, manifest = _validate_fixture(args.fixture)
    attestation = _attestation(args)
    boundary = _effective_boundary_preflight()
    order = args.order
    if order == "random":
        order = random.choice(("ordinary-first", "fast-first"))
    compare_id = f"compare-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    compare_root = _state_root() / "runs" / compare_id
    compare_root.parent.mkdir(parents=True, exist_ok=True)
    _create_owned_run(compare_root, "comparison")

    with _fixture_lock(fixture):
        runners = {
            "ordinary": lambda: _run_baseline(
                fixture, manifest, args.model, args.effort, args.timeout, compare_root / "ordinary"
            ),
            "fast": lambda: _run_fast_locked(
                fixture,
                manifest,
                attestation,
                args.model,
                args.effort,
                args.timeout,
                compare_root / "fast",
                copyback=False,
            ),
        }
        names = ["ordinary", "fast"] if order == "ordinary-first" else ["fast", "ordinary"]
        receipts = {}
        for name in names:
            receipts[name] = runners[name]()

    output_hash_error = None
    outputs_equal = False
    if receipts["ordinary"]["status"] == "PASS" and receipts["fast"]["status"] == "PASS":
        try:
            ordinary_outputs = _output_hashes(
                Path(receipts["ordinary"]["staging_directory"]), manifest
            )
            fast_outputs = _output_hashes(Path(receipts["fast"]["staging_directory"]), manifest)
            outputs_equal = ordinary_outputs == fast_outputs
        except FastLaneError as exc:
            output_hash_error = str(exc)
    outcome = _comparison_outcome(receipts["ordinary"], receipts["fast"], outputs_equal)
    ordinary_usage = receipts["ordinary"].get("usage") or {}
    fast_usage = receipts["fast"].get("usage") or {}
    ordinary_input = ordinary_usage.get("input_tokens")
    fast_input = fast_usage.get("input_tokens")
    result = {
        "schema_version": 1,
        "comparison_id": compare_id,
        "outcome": outcome,
        "evidence_strength": "DIRECTIONAL_ONE_PAIR",
        "order": order,
        "task_id": manifest.get("task_id"),
        "model": args.model,
        "reasoning_effort": args.effort,
        "effective_boundary": boundary,
        "outputs_byte_identical": outputs_equal,
        "output_hash_error": output_hash_error,
        "ordinary": receipts["ordinary"],
        "fast": receipts["fast"],
        "difference": {
            "input_tokens": (
                fast_input - ordinary_input
                if isinstance(fast_input, int) and isinstance(ordinary_input, int)
                else None
            ),
            "input_token_change_percent": (
                _percent_change(fast_input, ordinary_input)
                if isinstance(fast_input, int) and isinstance(ordinary_input, int)
                else None
            ),
            "tool_calls": receipts["fast"]["tool_calls"] - receipts["ordinary"]["tool_calls"],
            "elapsed_seconds": round(
                receipts["fast"]["elapsed_seconds"] - receipts["ordinary"]["elapsed_seconds"], 3
            ),
            "elapsed_change_percent": _percent_change(
                receipts["fast"]["elapsed_seconds"], receipts["ordinary"]["elapsed_seconds"]
            ),
        },
        "interpretation": (
            "One counter-orderable pair is directional evidence, not an average savings claim. "
            "Repeat with the opposite order before making a stronger claim."
        ),
        "original_fixture_modified": False,
        "result_path": str(compare_root / "comparison.json"),
    }
    (compare_root / "comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if outcome != "INVALID_COMPARISON" else 1


def _add_attestations(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--semantic-self-containment", required=True, choices=("PASS",))
    parser.add_argument("--packet-complete", required=True, choices=("YES",))
    parser.add_argument("--external-dependency", required=True, choices=("NONE",))


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument(
        "--effort",
        default="medium",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument("--timeout", type=int, default=900)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install-profile", help="install the named Codex permission profile")
    install.add_argument("--force", action="store_true", help="back up and replace a different existing profile")
    install.set_defaults(handler=_install_profile)

    uninstall = commands.add_parser(
        "uninstall", help="remove only the profile and evidence owned by this beta"
    )
    uninstall.set_defaults(handler=_uninstall)

    preflight = commands.add_parser("preflight", help="validate a fixture without running a model")
    preflight.add_argument("fixture")
    _add_attestations(preflight)
    preflight.set_defaults(handler=_preflight)

    run = commands.add_parser("run", help="run one fast-lane task and copy back verified outputs")
    run.add_argument("fixture")
    _add_attestations(run)
    _add_model_options(run)
    run.set_defaults(handler=_run)

    compare = commands.add_parser("compare", help="run a safe ordinary/fast A/B pair without changing the source")
    compare.add_argument("fixture")
    _add_attestations(compare)
    _add_model_options(compare)
    compare.add_argument(
        "--order",
        default="random",
        choices=("random", "ordinary-first", "fast-first"),
        help="random by default; use the opposite order for a second pair",
    )
    compare.set_defaults(handler=_compare)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return args.handler(args)
    except (FastLaneError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"STOPPED_NOT_RUNNING: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
