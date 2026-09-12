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
import signal
import stat
import subprocess
import sys
import sysconfig
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


def _shipped_profile_path() -> Path:
    """Locate the reviewed profile in a source checkout or installed wheel."""
    relative = Path("profiles") / f"{PROFILE_ID}.config.toml"
    source_candidate = _package_root() / relative
    if source_candidate.is_file():
        return source_candidate
    installed_candidate = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "codex-task-local-fast-lane"
        / relative
    )
    if installed_candidate.is_file():
        return installed_candidate
    raise FastLaneError(
        "shipped permission profile is missing from both the source checkout and installed package: "
        f"{source_candidate}, {installed_candidate}"
    )


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
    _ensure_durable_directory(root)
    if not marker.exists():
        _durable_atomic_write(
            marker,
            (
                json.dumps(
                    {"owner": "codex-task-local-fast-lane", "schema_version": 1},
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FastLaneError(f"invalid state ownership marker: {marker}") from exc
    if payload != {"owner": "codex-task-local-fast-lane", "schema_version": 1}:
        raise FastLaneError(f"state ownership marker does not match this beta: {marker}")
    return root


def _create_owned_run(path: Path, kind: str) -> None:
    _create_durable_directory(path)
    _durable_atomic_write(
        path / RUN_MARKER,
        (
            json.dumps(
                {"owner": "codex-task-local-fast-lane", "schema_version": 1, "kind": kind},
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
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


def _stat_identity(value: os.stat_result) -> dict[str, int]:
    """Return the stable local identity fields needed for fail-closed writes."""
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "file_type": stat.S_IFMT(value.st_mode),
    }


def _lstat_identity(path: Path) -> dict[str, int] | None:
    try:
        return _stat_identity(path.lstat())
    except FileNotFoundError:
        return None


def _identity_at(directory_descriptor: int, name: str) -> dict[str, int] | None:
    try:
        return _stat_identity(
            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        )
    except FileNotFoundError:
        return None


def _capture_copyback_path_identity(fixture: Path, manifest: dict) -> dict:
    """Bind copyback to the fixture and directory tree seen with its preimages."""
    fixture_identity = _lstat_identity(fixture)
    if fixture_identity is None or fixture_identity["file_type"] != stat.S_IFDIR:
        raise FastLaneError("COPYBACK_CONFLICT: fixture is not a stable directory")
    directories: dict[str, dict[str, int] | None] = {".": fixture_identity}
    destinations: dict[str, dict[str, int] | None] = {}
    for rel in manifest["allowed_writes"]:
        current = fixture
        parts: list[str] = []
        for part in rel.parts[:-1]:
            parts.append(part)
            current = current / part
            raw = PurePosixPath(*parts).as_posix()
            identity = _lstat_identity(current)
            if identity is not None and identity["file_type"] != stat.S_IFDIR:
                raise FastLaneError(
                    f"COPYBACK_CONFLICT: output parent is not a directory: {raw}"
                )
            directories.setdefault(raw, identity)
        target_identity = _lstat_identity(fixture / rel)
        if target_identity is not None and target_identity["file_type"] != stat.S_IFREG:
            raise FastLaneError(
                f"COPYBACK_CONFLICT: destination is not a regular file: {rel.as_posix()}"
            )
        destinations[rel.as_posix()] = target_identity
    return {
        "fixture": fixture_identity,
        "directories": directories,
        "destinations": destinations,
    }


def _verify_copyback_path_identity(
    fixture: Path,
    snapshot: dict,
    created_directory_identities: dict[str, dict[str, int]] | None = None,
) -> None:
    created = created_directory_identities or {}
    actual_fixture = _lstat_identity(fixture)
    if actual_fixture != snapshot["fixture"]:
        raise FastLaneError("COPYBACK_CONFLICT: fixture path identity changed")
    for raw, original in snapshot["directories"].items():
        expected = created.get(raw, original)
        candidate = fixture if raw == "." else fixture / PurePosixPath(raw)
        actual = _lstat_identity(candidate)
        if actual != expected:
            raise FastLaneError(
                f"COPYBACK_CONFLICT: output directory identity changed: {raw}"
            )


def _open_bound_directory(path: Path, expected: dict[str, int], label: str) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise FastLaneError("no-follow directory handles are unavailable on this platform")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FastLaneError(f"COPYBACK_CONFLICT: cannot open {label} without following links") from exc
    if _stat_identity(os.fstat(descriptor)) != expected:
        os.close(descriptor)
        raise FastLaneError(f"COPYBACK_CONFLICT: {label} identity changed while opening")
    return descriptor


def _open_copyback_parent(
    fixture_descriptor: int,
    parent: PurePosixPath,
    snapshot: dict,
    created_directory_identities: dict[str, dict[str, int]],
    created_directory_handles: list[tuple[int, str, str]],
) -> int:
    current_descriptor = os.dup(fixture_descriptor)
    parts: list[str] = []
    try:
        for part in parent.parts:
            if part == ".":
                continue
            parts.append(part)
            raw = PurePosixPath(*parts).as_posix()
            original = snapshot["directories"].get(raw)
            expected = created_directory_identities.get(raw, original)
            if expected is None:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_descriptor)
                    _fsync_directory_descriptor(current_descriptor)
                except FileExistsError as exc:
                    raise FastLaneError(
                        f"COPYBACK_CONFLICT: output directory appeared during copyback: {raw}"
                    ) from exc
                created_directory_handles.append((os.dup(current_descriptor), part, raw))
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                next_descriptor = os.open(part, flags, dir_fd=current_descriptor)
            except OSError as exc:
                raise FastLaneError(
                    f"COPYBACK_CONFLICT: cannot open output directory without following links: {raw}"
                ) from exc
            actual = _stat_identity(os.fstat(next_descriptor))
            if expected is None:
                created_directory_identities[raw] = actual
            elif actual != expected:
                os.close(next_descriptor)
                raise FastLaneError(
                    f"COPYBACK_CONFLICT: output directory identity changed while opening: {raw}"
                )
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        return current_descriptor
    except Exception:
        os.close(current_descriptor)
        raise


def _hash_at(directory_descriptor: int, name: str) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise FastLaneError(f"refusing to hash non-regular path: {name}")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_bytes_at(directory_descriptor: int, name: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise FastLaneError(f"refusing to read non-regular path: {name}")
        return handle.read()


def _copy_regular_file_to_new_at(
    source: Path | tuple[int, str],
    target_directory_descriptor: int,
    target_name: str,
    mode: int,
) -> None:
    read_flags = os.O_RDONLY | os.O_NOFOLLOW
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if isinstance(source, tuple):
        source_descriptor = os.open(source[1], read_flags, dir_fd=source[0])
    else:
        source_descriptor = os.open(source, read_flags)
    try:
        target_descriptor = os.open(
            target_name,
            write_flags,
            mode,
            dir_fd=target_directory_descriptor,
        )
    except Exception:
        os.close(source_descriptor)
        raise
    with (
        os.fdopen(source_descriptor, "rb") as source_handle,
        os.fdopen(target_descriptor, "wb") as target_handle,
    ):
        if not stat.S_ISREG(os.fstat(source_handle.fileno()).st_mode):
            raise FastLaneError("copy source is not a regular file")
        shutil.copyfileobj(source_handle, target_handle)
        target_handle.flush()
        os.fchmod(target_handle.fileno(), mode)
        os.fsync(target_handle.fileno())


def _replace_at(directory_descriptor: int, source_name: str, destination_name: str) -> None:
    os.replace(
        source_name,
        destination_name,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
    )


def _fsync_directory_descriptor(directory_descriptor: int) -> None:
    """Persist directory-entry changes before recording a durable state transition."""
    os.fsync(directory_descriptor)


def _durable_atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Atomically replace a regular file and durably publish the new directory entry."""
    parent_identity = _lstat_identity(path.parent)
    if parent_identity is None or parent_identity["file_type"] != stat.S_IFDIR:
        raise FastLaneError(f"durable write parent is not a directory: {path.parent}")
    parent_descriptor = _open_bound_directory(
        path.parent, parent_identity, "durable write parent"
    )
    temporary_name = path.name + f".tmp-{uuid.uuid4().hex}"
    temporary_created = False
    try:
        existing = _identity_at(parent_descriptor, path.name)
        if existing is not None and existing["file_type"] != stat.S_IFREG:
            raise FastLaneError(f"durable write destination is not a regular file: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(
            temporary_name,
            flags,
            mode,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        _replace_at(parent_descriptor, temporary_name, path.name)
        temporary_created = False
        _fsync_directory_descriptor(parent_descriptor)
    finally:
        if temporary_created and _identity_at(parent_descriptor, temporary_name) is not None:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            _fsync_directory_descriptor(parent_descriptor)
        os.close(parent_descriptor)


def _create_durable_directory(path: Path) -> None:
    """Create one directory after durably establishing its missing ancestors."""
    _ensure_durable_directory(path.parent)
    parent_identity = _lstat_identity(path.parent)
    if parent_identity is None or parent_identity["file_type"] != stat.S_IFDIR:
        raise FastLaneError(f"directory parent is not stable: {path.parent}")
    parent_descriptor = _open_bound_directory(
        path.parent, parent_identity, "directory parent"
    )
    child_descriptor = None
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
        child_identity = _identity_at(parent_descriptor, path.name)
        if child_identity is None or child_identity["file_type"] != stat.S_IFDIR:
            raise FastLaneError(f"created path is not a directory: {path}")
        child_descriptor = _open_bound_directory(path, child_identity, "created directory")
        _fsync_directory_descriptor(child_descriptor)
        _fsync_directory_descriptor(parent_descriptor)
    finally:
        if child_descriptor is not None:
            os.close(child_descriptor)
        os.close(parent_descriptor)


def _ensure_durable_directory(path: Path) -> None:
    """Create every missing ancestor and durably publish each directory entry."""
    missing: list[Path] = []
    cursor = path
    while True:
        identity = _lstat_identity(cursor)
        if identity is not None:
            if identity["file_type"] != stat.S_IFDIR:
                raise FastLaneError(f"path is not a directory: {cursor}")
            break
        parent = cursor.parent
        if parent == cursor:
            raise FastLaneError(f"cannot find an existing directory ancestor for: {path}")
        missing.append(cursor)
        cursor = parent

    for candidate in reversed(missing):
        parent_identity = _lstat_identity(candidate.parent)
        if parent_identity is None or parent_identity["file_type"] != stat.S_IFDIR:
            raise FastLaneError(f"directory parent changed while creating: {candidate.parent}")
        parent_descriptor = _open_bound_directory(
            candidate.parent, parent_identity, "directory parent"
        )
        child_descriptor = None
        try:
            os.mkdir(candidate.name, mode=0o700, dir_fd=parent_descriptor)
            child_identity = _identity_at(parent_descriptor, candidate.name)
            if child_identity is None or child_identity["file_type"] != stat.S_IFDIR:
                raise FastLaneError(f"created path is not a directory: {candidate}")
            child_descriptor = _open_bound_directory(
                candidate, child_identity, "created directory"
            )
            _fsync_directory_descriptor(child_descriptor)
            _fsync_directory_descriptor(parent_descriptor)
        finally:
            if child_descriptor is not None:
                os.close(child_descriptor)
            os.close(parent_descriptor)


@contextmanager
def _fixture_lock(fixture: Path):
    lock_root = _ensure_state_root() / "locks"
    _ensure_durable_directory(lock_root)
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


def _require_no_unfinished_copyback(fixture: Path) -> None:
    """Refuse a new fixture run when an earlier copyback did not end cleanly."""
    runs_root = _ensure_state_root() / "runs"
    if not runs_root.exists():
        return
    terminal = {"COMMITTED", "ROLLED_BACK"}
    blockers = []
    for journal in sorted(runs_root.rglob("copyback-transaction.json")):
        if journal.is_symlink():
            raise FastLaneError(
                f"RECOVERY_REQUIRED: transaction journal is a symlink: {journal}"
            )
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FastLaneError(
                f"RECOVERY_REQUIRED: transaction journal is unreadable: {journal}"
            ) from exc
        try:
            _validate_copyback_journal(payload, journal)
        except FastLaneError as exc:
            raise FastLaneError(
                f"RECOVERY_REQUIRED: transaction journal is invalid: {journal}: {exc}"
            ) from exc
        if payload.get("fixture") != str(fixture):
            continue
        status = payload.get("status")
        if status not in terminal:
            blockers.append({"journal": str(journal), "status": status or "UNKNOWN"})
    if blockers:
        raise FastLaneError(
            "RECOVERY_REQUIRED: unfinished copyback exists for this fixture; "
            "inspect the journal and recovery artifacts before removing the fence: "
            + json.dumps(blockers, sort_keys=True)
        )


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


def _terminate_process_tree(
    process: subprocess.Popen, process_group: int | None = None
) -> dict:
    """Terminate the launched command boundary and return literal cleanup evidence.

    A POSIX process group is useful cleanup containment, but it is not proof of
    an arbitrary descendant tree: a descendant can create a new session/group.
    Keep that distinction explicit instead of claiming more than the launcher
    can establish.
    """
    evidence = {
        "strategy": "none",
        "term_sent": False,
        "kill_sent": False,
        "cleanup_confirmed": False,
        "original_process_group_cleanup_confirmed": False,
        "descendant_containment_proven": False,
        "errors": [],
        "uncertainties": [],
    }
    if process.poll() is not None and os.name != "posix":
        evidence["strategy"] = "already-exited"
        evidence["cleanup_confirmed"] = True
        return evidence

    if os.name == "posix":
        evidence["strategy"] = "posix-process-group"
        process_group = process_group or process.pid
        group_exists = True
        try:
            os.killpg(process_group, signal.SIGTERM)
            evidence["term_sent"] = True
        except ProcessLookupError:
            group_exists = False
        except OSError as exc:
            evidence["errors"].append(f"SIGTERM:{exc}")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

        # The direct child may exit after SIGTERM while a descendant ignores it.
        # Probe the original process group rather than treating the child exit as
        # proof that every inherited pipe and subprocess is gone.
        if group_exists:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                group_exists = False
            except OSError as exc:
                group_exists = True
                evidence["errors"].append(f"GROUP_PROBE:{exc}")
            else:
                group_exists = True
        if group_exists:
            try:
                os.killpg(process_group, signal.SIGKILL)
                evidence["kill_sent"] = True
            except ProcessLookupError:
                group_exists = False
            except OSError as exc:
                evidence["errors"].append(f"SIGKILL:{exc}")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            evidence["errors"].append("direct process did not exit after group termination")

        deadline = time.monotonic() + 2
        while group_exists and time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                group_exists = False
                break
            except OSError:
                pass
            time.sleep(0.05)
        if group_exists:
            evidence["errors"].append("process group still exists after SIGKILL")
        evidence["original_process_group_cleanup_confirmed"] = (
            process.poll() is not None and not group_exists and not evidence["errors"]
        )
        evidence["uncertainties"].append(
            "POSIX process-group cleanup cannot prove termination of descendants "
            "that detach with setsid/setpgid"
        )
        # A timeout always fails the arm (exit 124), so copyback remains closed.
        # Keep the stronger whole-tree claim false even when the original group
        # was demonstrably removed.
        evidence["cleanup_confirmed"] = False
        return evidence
    elif os.name == "nt":
        evidence["strategy"] = "windows-taskkill-tree"
        killed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            text=True,
            capture_output=True,
            check=False,
        )
        evidence["kill_sent"] = True
        if killed.returncode != 0:
            evidence["errors"].append(
                f"taskkill returned {killed.returncode}: {killed.stderr.strip()}"
            )
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            evidence["errors"].append("process tree did not exit after taskkill")
    else:
        evidence["strategy"] = "direct-process-fallback"
        try:
            process.kill()
            evidence["kill_sent"] = True
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as exc:
            evidence["errors"].append(str(exc))

    evidence["cleanup_confirmed"] = process.poll() is not None and not evidence["errors"]
    evidence["descendant_containment_proven"] = evidence["cleanup_confirmed"]
    return evidence


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
    popen_options = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **popen_options,
    )
    process_group = process.pid if os.name == "posix" else None
    timed_out = False
    termination = None
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        termination = _terminate_process_tree(process, process_group)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            termination["cleanup_confirmed"] = False
            termination["errors"].append("pipes remained open after process-tree cleanup")
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
        marker = "FAST_LANE_TIMEOUT " + json.dumps(termination, sort_keys=True)
        stderr = (stderr or "") + ("\n" if stderr else "") + marker + "\n"
    completed = subprocess.CompletedProcess(
        argv,
        124 if timed_out else process.returncode,
        stdout or "",
        stderr or "",
    )
    completed.timed_out = timed_out
    completed.termination = termination
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
    identity = _lstat_identity(path)
    if identity is None or identity["file_type"] != stat.S_IFREG:
        raise FastLaneError(
            f"permission profile is missing or not a regular file at {path}; run: "
            "python3 fastlane.py install-profile"
        )
    shipped = _shipped_profile_path()
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


def _sandbox_probe_python_candidates() -> list[str]:
    """Return distinct Python executables that may be readable inside the profile.

    The CLI itself is commonly installed in a virtual environment outside the
    profile's read-only toolchain roots. Using only ``sys.executable`` would
    therefore make a correct boundary look broken. Keep the current runtime as
    the first candidate, then try every executable Python exposed by PATH.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        try:
            resolved = str(Path(raw).expanduser().resolve(strict=True))
        except OSError:
            return
        if resolved in seen or not os.access(resolved, os.X_OK):
            return
        seen.add(resolved)
        candidates.append(resolved)

    add(sys.executable)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in ("python3", "python"):
            add(str(Path(directory) / name))
    return candidates


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
    _ensure_durable_directory(runs_root)
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
    probe_python = None
    try:
        for candidate in _sandbox_probe_python_candidates():
            allowed_path = workspace / "allowed.txt"
            allowed_path.unlink(missing_ok=True)
            allowed, _ = _run_command(
                base
                + [
                    candidate,
                    "-c",
                    "from pathlib import Path; Path('allowed.txt').write_text('ok')",
                ],
                workspace,
                30,
            )
            if allowed.returncode == 0 and allowed_path.is_file():
                probe_python = candidate
                break
        probes["workspace_write_allowed"] = probe_python is not None
        if probe_python is None:
            raise FastLaneError(
                "effective permission boundary probe could not execute any Python from the reviewed toolchain"
            )

        denied_read, _ = _run_command(
            base
            + [
                probe_python,
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
                probe_python,
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
                probe_python,
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
                probe_python,
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
        "probe_python": probe_python,
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
        "timed_out": completed.timed_out,
        "timeout_cleanup": completed.termination,
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
        "timed_out": completed.timed_out,
        "timeout_cleanup": completed.termination,
        "elapsed_seconds": elapsed,
        "stdout": str(run_root / f"{label}.verification.stdout.txt"),
        "stderr": str(run_root / f"{label}.verification.stderr.txt"),
    }


def _write_copyback_journal(run_root: Path, payload: dict) -> None:
    path = run_root / "copyback-transaction.json"
    document = dict(payload)
    document.pop("journal_sha256", None)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    document["journal_sha256"] = hashlib.sha256(canonical).hexdigest()
    _durable_atomic_write(
        path,
        (json.dumps(document, indent=2) + "\n").encode("utf-8"),
    )


def _seal_terminal_copyback(run_root: Path) -> None:
    journal_path = run_root / "copyback-transaction.json"
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    _validate_copyback_journal(payload, journal_path, require_terminal_seal=False)
    if payload.get("status") not in {"COMMITTED", "ROLLED_BACK"}:
        raise FastLaneError("cannot seal a non-terminal copyback journal")
    fixture = Path(payload["fixture"])
    resolved_fixture = fixture.resolve(strict=True)
    if str(resolved_fixture) != payload["fixture"]:
        raise FastLaneError("terminal journal fixture is not canonical")
    _verify_copyback_path_identity(
        resolved_fixture,
        payload["path_identity"],
        payload["created_directory_identities"],
    )
    actual_terminal_hashes = {}
    for raw in payload["terminal_source_hashes"]:
        rel = _relative_path(raw, "terminal source hash")
        candidate = resolved_fixture / rel
        if _path_has_symlink(resolved_fixture, rel):
            raise FastLaneError(f"terminal source path uses a symlink: {raw}")
        actual_terminal_hashes[raw] = _hash(candidate) if candidate.is_file() else None
    if actual_terminal_hashes != payload["terminal_source_hashes"]:
        raise FastLaneError("live fixture bytes do not match terminal source hashes")
    marker_path = run_root / RUN_MARKER
    _validate_owned_run(run_root)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["terminal_copyback_status"] = payload["status"]
    marker["terminal_copyback_journal_sha256"] = payload["journal_sha256"]
    _durable_atomic_write(
        marker_path,
        (json.dumps(marker, indent=2) + "\n").encode("utf-8"),
    )


def _validate_copyback_journal(
    payload: dict, path: Path, *, require_terminal_seal: bool = True
) -> None:
    if not isinstance(payload, dict):
        raise FastLaneError("journal root is not an object")
    supplied_digest = payload.get("journal_sha256")
    unsigned = dict(payload)
    unsigned.pop("journal_sha256", None)
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_digest = hashlib.sha256(canonical).hexdigest()
    if supplied_digest != expected_digest:
        raise FastLaneError("journal digest mismatch")
    if payload.get("schema_version") != 1:
        raise FastLaneError("unsupported journal schema")
    if payload.get("transaction_id") != path.parent.name:
        raise FastLaneError("transaction id does not match journal directory")
    fixture_raw = payload.get("fixture")
    if not isinstance(fixture_raw, str) or not Path(fixture_raw).is_absolute():
        raise FastLaneError("journal fixture is not an absolute path")
    status_value = payload.get("status")
    known_statuses = {
        "PREPARING",
        "COMMITTING",
        "COMMITTED",
        "ROLLED_BACK",
        "ROLLBACK_FAILED",
        "RECOVERY_REQUIRED",
    }
    if status_value not in known_statuses:
        raise FastLaneError("unknown journal status")

    allowed_writes = payload.get("allowed_writes")
    source_preimages = payload.get("source_preimages")
    prepared = payload.get("prepared_files")
    replaced = payload.get("replaced_files")
    restored = payload.get("restored_files")
    artifacts = payload.get("artifacts")
    path_identity = payload.get("path_identity")
    created_directory_identities = payload.get("created_directory_identities")
    if not isinstance(allowed_writes, list) or not allowed_writes:
        raise FastLaneError("allowed_writes missing")
    if not isinstance(source_preimages, dict) or not source_preimages:
        raise FastLaneError("source_preimages missing")
    if not all(isinstance(value, list) for value in (prepared, replaced, restored, artifacts)):
        raise FastLaneError("transaction lists are malformed")

    def valid_identity(value) -> bool:
        return isinstance(value, dict) and set(value) == {"device", "inode", "file_type"} and all(
            isinstance(value[key], int) and not isinstance(value[key], bool)
            for key in ("device", "inode", "file_type")
        )

    if not isinstance(path_identity, dict) or not valid_identity(path_identity.get("fixture")):
        raise FastLaneError("copyback path identity is missing or malformed")
    directory_identities = path_identity.get("directories")
    destination_identities = path_identity.get("destinations")
    if not isinstance(directory_identities, dict) or not valid_identity(
        directory_identities.get(".")
    ):
        raise FastLaneError("copyback directory identities are missing or malformed")
    if not isinstance(destination_identities, dict):
        raise FastLaneError("copyback destination identities are missing or malformed")
    if not isinstance(created_directory_identities, dict):
        raise FastLaneError("created directory identities are missing or malformed")
    for raw, identity in directory_identities.items():
        if raw != ".":
            _relative_path(raw, "journal directory identity")
        if identity is not None and not valid_identity(identity):
            raise FastLaneError("copyback directory identity is malformed")
    for raw, identity in created_directory_identities.items():
        _relative_path(raw, "journal created directory identity")
        if raw not in directory_identities or directory_identities[raw] is not None:
            raise FastLaneError("created directory identity was not absent at startup")
        if not valid_identity(identity):
            raise FastLaneError("created directory identity is malformed")

    def valid_hash(value) -> bool:
        return value is None or (
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )

    for raw in allowed_writes:
        rel = _relative_path(raw, "journal allowed_write")
        if raw not in destination_identities:
            raise FastLaneError("copyback destination identity is incomplete")
        identity = destination_identities[raw]
        if identity is not None and not valid_identity(identity):
            raise FastLaneError("copyback destination identity is malformed")
        parts = []
        for part in rel.parts[:-1]:
            parts.append(part)
            if PurePosixPath(*parts).as_posix() not in directory_identities:
                raise FastLaneError("copyback parent identity is incomplete")
    if set(destination_identities) != set(allowed_writes):
        raise FastLaneError("copyback destination identities exceed allowed writes")
    for raw, value in source_preimages.items():
        _relative_path(raw, "journal source_preimage")
        if not valid_hash(value):
            raise FastLaneError("source_preimages contains an invalid hash")
    for values, label in (
        (prepared, "prepared_files"),
        (replaced, "replaced_files"),
        (restored, "restored_files"),
    ):
        if len(values) != len(set(values)):
            raise FastLaneError(f"{label} contains duplicates")
        for raw in values:
            _relative_path(raw, f"journal {label}")
    if not set(replaced).issubset(set(prepared)) or not set(restored).issubset(set(replaced)):
        raise FastLaneError("transaction file-state sets are inconsistent")
    artifact_paths = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise FastLaneError("artifact entry is malformed")
        artifact_paths.append(artifact["path"])
    if artifact_paths != prepared:
        raise FastLaneError("artifact paths do not match prepared files")

    if status_value in {"COMMITTED", "ROLLED_BACK"}:
        terminal_hashes = payload.get("terminal_source_hashes")
        if not isinstance(payload.get("terminal_recorded_at_utc"), str):
            raise FastLaneError("terminal timestamp missing")
        if not isinstance(terminal_hashes, dict) or set(terminal_hashes) != set(source_preimages):
            raise FastLaneError("terminal source hashes missing or incomplete")
        if not all(valid_hash(value) for value in terminal_hashes.values()):
            raise FastLaneError("terminal source hashes contain an invalid hash")
        if status_value == "COMMITTED":
            if set(prepared) != set(allowed_writes) or set(replaced) != set(allowed_writes):
                raise FastLaneError("committed journal does not cover every allowed write")
            if restored:
                raise FastLaneError("committed journal unexpectedly records restored files")
        elif set(replaced) != set(restored) or terminal_hashes != source_preimages:
            raise FastLaneError("rolled-back journal does not prove full preimage restoration")
        if require_terminal_seal:
            marker_path = path.parent / RUN_MARKER
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FastLaneError("terminal run-marker seal is unreadable") from exc
            if (
                marker.get("owner") != "codex-task-local-fast-lane"
                or marker.get("schema_version") != 1
                or marker.get("terminal_copyback_status") != status_value
                or marker.get("terminal_copyback_journal_sha256") != supplied_digest
            ):
                raise FastLaneError("terminal run-marker seal is missing or mismatched")


def _transactional_copyback(
    *,
    stage: Path,
    fixture: Path,
    manifest: dict,
    preimages: dict[str, str | None],
    run_root: Path,
    path_identity: dict | None = None,
) -> dict:
    """Prepare every output first, then commit all or roll back prior replacements."""
    path_identity = path_identity or _capture_copyback_path_identity(fixture, manifest)
    _verify_copyback_path_identity(fixture, path_identity)
    fixture_descriptor = _open_bound_directory(
        fixture, path_identity["fixture"], "fixture directory"
    )
    created_directory_identities: dict[str, dict[str, int]] = {}
    created_directory_handles: list[tuple[int, str, str]] = []
    transaction = {
        "schema_version": 1,
        "transaction_id": run_root.name,
        "fixture": str(fixture),
        "allowed_writes": [path.as_posix() for path in manifest["allowed_writes"]],
        "source_preimages": preimages,
        "path_identity": path_identity,
        "created_directory_identities": created_directory_identities,
        "status": "PREPARING",
        "prepared_files": [],
        "artifacts": [],
        "replaced_files": [],
        "restored_files": [],
        "created_directories": [],
        "cleanup_errors": [],
        "recovery_artifacts": [],
        "error": None,
    }
    _write_copyback_journal(run_root, transaction)
    prepared = []
    replaced = []
    try:
        for rel in manifest["allowed_writes"]:
            source = _require_regular_contained_file(stage, rel, "staged output before copyback")
            destination = fixture / rel
            parent_rel = PurePosixPath(*rel.parts[:-1])
            parent_descriptor = _open_copyback_parent(
                fixture_descriptor,
                parent_rel,
                path_identity,
                created_directory_identities,
                created_directory_handles,
            )
            expected_destination = path_identity["destinations"][rel.as_posix()]
            actual_destination = _identity_at(parent_descriptor, rel.name)
            if actual_destination != expected_destination:
                os.close(parent_descriptor)
                raise FastLaneError(
                    f"COPYBACK_CONFLICT: destination identity changed: {rel.as_posix()}"
                )

            if actual_destination is not None:
                destination_mode = os.stat(
                    rel.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                ).st_mode
                target_mode = stat.S_IMODE(destination_mode)
            else:
                target_mode = stat.S_IMODE(source.lstat().st_mode)

            token = uuid.uuid4().hex
            temporary_name = rel.name + f".fast-lane-{token}.tmp"
            backup_name = rel.name + f".fast-lane-{token}.bak" if actual_destination else None
            temporary = destination.with_name(temporary_name)
            backup = destination.with_name(backup_name) if backup_name else None
            item = {
                "rel": rel,
                "destination": destination,
                "parent_descriptor": parent_descriptor,
                "destination_name": rel.name,
                "expected_destination_identity": expected_destination,
                "temporary": temporary,
                "temporary_name": temporary_name,
                "backup": backup,
                "backup_name": backup_name,
                "mode": target_mode,
            }
            prepared.append(item)
            _copy_regular_file_to_new_at(
                source, parent_descriptor, temporary_name, target_mode
            )
            if _hash_at(parent_descriptor, temporary_name) != _hash(source):
                raise FastLaneError(f"staged output changed during preparation: {rel.as_posix()}")
            if backup_name is not None:
                _copy_regular_file_to_new_at(
                    (parent_descriptor, rel.name),
                    parent_descriptor,
                    backup_name,
                    target_mode,
                )
            _fsync_directory_descriptor(parent_descriptor)
            transaction["prepared_files"].append(rel.as_posix())
            transaction["artifacts"].append(
                {
                    "path": rel.as_posix(),
                    "destination": str(destination),
                    "temporary": str(temporary),
                    "backup": str(backup) if backup is not None else None,
                    "mode": oct(target_mode),
                }
            )
            _write_copyback_journal(run_root, transaction)

        _verify_copyback_path_identity(
            fixture, path_identity, created_directory_identities
        )
        late_conflicts = _preimage_conflicts(fixture, preimages)
        if late_conflicts:
            raise FastLaneError(f"COPYBACK_CONFLICT: {sorted(late_conflicts)}")

        transaction["status"] = "COMMITTING"
        transaction["created_directories"] = list(created_directory_identities)
        _write_copyback_journal(run_root, transaction)
        for item in prepared:
            rel = item["rel"]
            parent_descriptor = item["parent_descriptor"]
            if _identity_at(parent_descriptor, item["destination_name"]) != item[
                "expected_destination_identity"
            ]:
                raise FastLaneError(
                    f"COPYBACK_CONFLICT: destination changed before replace: {rel.as_posix()}"
                )
            _replace_at(
                parent_descriptor,
                item["temporary_name"],
                item["destination_name"],
            )
            item["committed_destination_identity"] = _identity_at(
                parent_descriptor, item["destination_name"]
            )
            replaced.append(item)
            transaction["replaced_files"].append(rel.as_posix())
            _fsync_directory_descriptor(parent_descriptor)
            _write_copyback_journal(run_root, transaction)

        transaction["status"] = "COMMITTED"
    except Exception as exc:
        transaction["error"] = f"{type(exc).__name__}: {exc}"
        rollback_errors = []
        for item in reversed(replaced):
            try:
                parent_descriptor = item["parent_descriptor"]
                destination_name = item["destination_name"]
                if _identity_at(parent_descriptor, destination_name) != item[
                    "committed_destination_identity"
                ]:
                    raise FastLaneError("destination identity changed before rollback")
                if item["backup_name"] is None:
                    os.unlink(destination_name, dir_fd=parent_descriptor)
                else:
                    _replace_at(
                        parent_descriptor,
                        item["backup_name"],
                        destination_name,
                    )
                _fsync_directory_descriptor(parent_descriptor)
                transaction["restored_files"].append(item["rel"].as_posix())
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"{item['rel'].as_posix()}:{type(rollback_exc).__name__}:{rollback_exc}"
                )
        transaction["cleanup_errors"].extend(rollback_errors)
        transaction["status"] = "ROLLBACK_FAILED" if rollback_errors else "ROLLED_BACK"
    finally:
        if transaction["status"] in {"COMMITTED", "ROLLED_BACK"}:
            try:
                _verify_copyback_path_identity(
                    fixture, path_identity, created_directory_identities
                )
            except FastLaneError as identity_exc:
                transaction["status"] = "RECOVERY_REQUIRED"
                transaction["error"] = transaction["error"] or str(identity_exc)
                transaction["cleanup_errors"].append(
                    f"PATH_IDENTITY:{type(identity_exc).__name__}:{identity_exc}"
                )
        unresolved_replacements = {
            item["rel"].as_posix() for item in replaced
        } - set(transaction["restored_files"])
        for item in prepared:
            parent_descriptor = item["parent_descriptor"]
            for path, name in (
                (item["temporary"], item["temporary_name"]),
                (item["backup"], item["backup_name"]),
            ):
                if name is None or _identity_at(parent_descriptor, name) is None:
                    continue
                if (
                    transaction["status"] in {"ROLLBACK_FAILED", "RECOVERY_REQUIRED"}
                    and name == item["backup_name"]
                    and item["rel"].as_posix() in unresolved_replacements
                ):
                    transaction["recovery_artifacts"].append(str(path))
                    continue
                try:
                    os.unlink(name, dir_fd=parent_descriptor)
                    _fsync_directory_descriptor(parent_descriptor)
                except OSError as cleanup_exc:
                    transaction["cleanup_errors"].append(
                        f"{path.name}:{type(cleanup_exc).__name__}:{cleanup_exc}"
                    )
                    if transaction["status"] in {"COMMITTED", "ROLLED_BACK"}:
                        transaction["status"] = "RECOVERY_REQUIRED"
                        transaction["error"] = transaction["error"] or (
                            "copyback artifact cleanup was not durably recorded"
                        )
        if transaction["status"] != "COMMITTED":
            for parent_descriptor, name, _raw in reversed(created_directory_handles):
                try:
                    os.rmdir(name, dir_fd=parent_descriptor)
                    _fsync_directory_descriptor(parent_descriptor)
                except OSError as cleanup_exc:
                    transaction["cleanup_errors"].append(
                        f"{name}:{type(cleanup_exc).__name__}:{cleanup_exc}"
                    )
                    if transaction["status"] == "ROLLED_BACK":
                        transaction["status"] = "RECOVERY_REQUIRED"
                        transaction["error"] = transaction["error"] or (
                            "created-directory rollback was not durably recorded"
                        )
        transaction["created_directories"] = list(created_directory_identities)
        if transaction["status"] in {"COMMITTED", "ROLLED_BACK"}:
            transaction["terminal_source_hashes"] = {
                raw: _hash(fixture / PurePosixPath(raw))
                if (fixture / PurePosixPath(raw)).is_file()
                else None
                for raw in preimages
            }
            transaction["terminal_recorded_at_utc"] = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
        try:
            _write_copyback_journal(run_root, transaction)
        except OSError as journal_exc:
            transaction["cleanup_errors"].append(
                f"FINAL_JOURNAL:{type(journal_exc).__name__}:{journal_exc}"
            )
            transaction["status"] = "RECOVERY_REQUIRED"
            transaction["error"] = transaction["error"] or "terminal journal write failed"
        if transaction["status"] in {"COMMITTED", "ROLLED_BACK"}:
            try:
                _seal_terminal_copyback(run_root)
            except (OSError, json.JSONDecodeError, FastLaneError) as seal_exc:
                transaction["status"] = "RECOVERY_REQUIRED"
                transaction["error"] = transaction["error"] or "terminal journal seal failed"
                transaction["cleanup_errors"].append(
                    f"TERMINAL_SEAL:{type(seal_exc).__name__}:{seal_exc}"
                )
                try:
                    _write_copyback_journal(run_root, transaction)
                except OSError as journal_exc:
                    transaction["cleanup_errors"].append(
                        f"RECOVERY_JOURNAL:{type(journal_exc).__name__}:{journal_exc}"
                    )
        for item in prepared:
            os.close(item["parent_descriptor"])
        for parent_descriptor, _name, _raw in created_directory_handles:
            os.close(parent_descriptor)
        os.close(fixture_descriptor)
    return transaction


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
    _create_owned_run(run_root, "fast-run")
    path_identity = _capture_copyback_path_identity(fixture, manifest)
    preimages = _source_preimages(fixture, manifest)
    _verify_copyback_path_identity(fixture, path_identity)
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
    copyback_transaction = None
    if status == "PASS" and copyback:
        copyback_transaction = _transactional_copyback(
            stage=stage,
            fixture=fixture,
            manifest=manifest,
            preimages=preimages,
            run_root=run_root,
            path_identity=path_identity,
        )
        if copyback_transaction["status"] != "COMMITTED":
            blockers.append(
                f"COPYBACK_{copyback_transaction['status']}: {copyback_transaction['error']}"
            )
            status = "FAIL"

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
        "copyback_transaction": copyback_transaction,
        "copyback_performed": bool(
            copyback_transaction and copyback_transaction["status"] == "COMMITTED"
        ),
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


def _comparison_outcome(ordinary: dict, fast: dict, results_equivalent: bool) -> str:
    ordinary_input = (ordinary.get("usage") or {}).get("input_tokens")
    fast_input = (fast.get("usage") or {}).get("input_tokens")
    valid = (
        ordinary.get("status") == "PASS"
        and fast.get("status") == "PASS"
        and results_equivalent
        and isinstance(ordinary_input, int)
        and isinstance(fast_input, int)
    )
    if not valid:
        return "INVALID_COMPARISON"
    return "HELPED" if fast_input < ordinary_input else "NO_CLEAR_GAIN"


def _usage_comparison(ordinary_usage: dict, fast_usage: dict) -> dict:
    """Keep every numeric usage counter separate instead of hiding cost components."""
    result = {}
    for key in sorted(set(ordinary_usage) | set(fast_usage)):
        ordinary_value = ordinary_usage.get(key)
        fast_value = fast_usage.get(key)
        if not isinstance(ordinary_value, (int, float)) or isinstance(ordinary_value, bool):
            continue
        if not isinstance(fast_value, (int, float)) or isinstance(fast_value, bool):
            continue
        result[key] = {
            "ordinary": ordinary_value,
            "fast": fast_value,
            "difference": fast_value - ordinary_value,
            "change_percent": _percent_change(fast_value, ordinary_value),
        }
    return result


def _install_profile(args: argparse.Namespace) -> int:
    version = _require_supported_codex()
    _ensure_state_root()
    source = _shipped_profile_path()
    destination = _profile_path()
    _ensure_durable_directory(destination.parent)
    resolved_parent = destination.parent.resolve(strict=True)
    destination = resolved_parent / destination.name
    parent_identity = _lstat_identity(resolved_parent)
    if parent_identity is None or parent_identity["file_type"] != stat.S_IFDIR:
        raise FastLaneError("profile directory is not a stable directory")
    parent_descriptor = _open_bound_directory(
        resolved_parent, parent_identity, "profile directory"
    )
    temporary_name = destination.name + f".install-{uuid.uuid4().hex}.tmp"
    backup_name = None
    try:
        existing_identity = _identity_at(parent_descriptor, destination.name)
        if existing_identity is not None and existing_identity["file_type"] != stat.S_IFREG:
            raise FastLaneError(
                f"profile destination is not a regular file: {destination}"
            )
        if existing_identity is not None and _read_bytes_at(
            parent_descriptor, destination.name
        ) == source.read_bytes():
            status = "ALREADY_INSTALLED"
        elif existing_identity is not None and not args.force:
            raise FastLaneError(
                f"profile already exists with different contents: {destination}; "
                "inspect it or rerun with --force"
            )
        else:
            target_mode = (
                stat.S_IMODE(
                    os.stat(
                        destination.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    ).st_mode
                )
                if existing_identity is not None
                else stat.S_IMODE(source.stat().st_mode)
            )
            if existing_identity is not None:
                backup_name = destination.name + ".bak-" + dt.datetime.now().strftime(
                    "%Y%m%d%H%M%S"
                ) + "-" + uuid.uuid4().hex[:8]
                _copy_regular_file_to_new_at(
                    (parent_descriptor, destination.name),
                    parent_descriptor,
                    backup_name,
                    target_mode,
                )
            _copy_regular_file_to_new_at(
                source, parent_descriptor, temporary_name, target_mode
            )
            if _identity_at(parent_descriptor, destination.name) != existing_identity:
                raise FastLaneError("profile destination identity changed before install")
            _replace_at(parent_descriptor, temporary_name, destination.name)
            if _lstat_identity(resolved_parent) != parent_identity:
                raise FastLaneError("profile directory identity changed during install")
            status = (
                f"UPDATED_WITH_BACKUP:{resolved_parent / backup_name}"
                if backup_name is not None
                else "INSTALLED"
            )
    finally:
        if _identity_at(parent_descriptor, temporary_name) is not None:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)
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
    source_profile = _shipped_profile_path()
    installed_profile = _profile_path()
    state = _state_root()

    installed_identity = _lstat_identity(installed_profile)
    if installed_identity is not None and installed_identity["file_type"] != stat.S_IFREG:
        raise FastLaneError(
            f"installed profile is not a regular file; refusing to remove: {installed_profile}"
        )
    if installed_identity is not None and installed_profile.read_bytes() != source_profile.read_bytes():
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
                for journal in path.rglob("copyback-transaction.json"):
                    try:
                        payload = json.loads(journal.read_text(encoding="utf-8"))
                        _validate_copyback_journal(payload, journal)
                    except (OSError, json.JSONDecodeError, FastLaneError) as exc:
                        raise FastLaneError(
                            f"RECOVERY_REQUIRED: refusing uninstall with invalid "
                            f"copyback evidence at {journal}: {exc}"
                        ) from exc
                    if payload.get("status") not in {"COMMITTED", "ROLLED_BACK"}:
                        raise FastLaneError(
                            f"RECOVERY_REQUIRED: refusing uninstall with unfinished "
                            f"copyback at {journal}"
                        )
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
    with _fixture_lock(fixture):
        _require_no_unfinished_copyback(fixture)
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
    with _fixture_lock(fixture):
        _require_no_unfinished_copyback(fixture)
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
    _create_owned_run(compare_root, "comparison")

    with _fixture_lock(fixture):
        _require_no_unfinished_copyback(fixture)
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
    results_equivalent = (
        receipts["ordinary"]["status"] == "PASS"
        and receipts["fast"]["status"] == "PASS"
        and output_hash_error is None
    )
    outcome = _comparison_outcome(receipts["ordinary"], receipts["fast"], results_equivalent)
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
        "results_equivalent": results_equivalent,
        "equivalence_basis": "BOTH_PASS_EXACT_VERIFIER_AND_CHANGED_FILE_SCOPE",
        "outputs_byte_identical": outputs_equal,
        "output_hash_error": output_hash_error,
        "ordinary": receipts["ordinary"],
        "fast": receipts["fast"],
        "difference": {
            "usage": _usage_comparison(ordinary_usage, fast_usage),
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
