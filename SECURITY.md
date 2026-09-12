# Security model

## Trust boundary

The launcher trusts the local operator to provide a genuinely disposable and
non-sensitive fixture. It does not classify source code, detect personal data,
or prove that a verifier is harmless.

The fast arm:

1. validates an explicit manifest;
2. rejects symlinked declared paths and parent traversal;
3. copies only declared task/runtime inputs into a generated Git boundary;
4. requests the `task-local-fast-lane` permission profile with local command
   network disabled;
5. disables known browser/app/plugin/memory/agent/hook features and every MCP
   server named in the user's base `config.toml`, and explicitly sets hosted
   `web_search = "disabled"` in both the reviewed profile and command override;
6. reads back the active permission profile and staging working directory from
   the local Codex rollout record;
7. runs the exact verifier under the same named permission profile;
8. rejects unexpected changed files, failed verification, missing changes, or
   source-preimage conflicts;
9. rejects post-model symlink outputs, symlink ancestors, non-regular outputs,
   and resolved paths outside the generated staging root before hashing, then
   repeats the output check immediately before copyback;
10. binds the fixture, output-parent directories, and existing destinations to
    their device/inode/file-type identities alongside the startup preimages;
11. prepares, backs up, and replaces outputs through no-follow directory
    handles rather than re-resolving mutable path strings, and returns
    `RECOVERY_REQUIRED` if the bound path tree changes;
12. prepares and hashes every manifest-allowlisted output before changing the
    fixture, preserves existing destination modes, and records a transaction
    journal while holding a per-fixture lock;
13. rolls back already-replaced outputs if a later replacement fails, and
    treats an incomplete rollback as a failed result requiring manual review;
14. starts verifier/model commands in a dedicated process group and records
    group-termination evidence when a timeout occurs; on POSIX it does not claim
    whole-tree cleanup for descendants that can detach with `setsid`/`setpgid`,
    so a timeout remains a failed, non-copyback result with containment marked
    unproven.
15. records the fixture and source preimages in a durable copyback journal and
    refuses every later preflight/compare/run for that fixture when the journal
    is non-terminal, preventing a launcher crash from silently redefining a
    half-committed source tree as the next clean baseline.
16. validates journal digest, schema, transaction identity, path identities,
    allowed writes, preimages, artifact/file-state consistency and terminal
    evidence before a terminal status can clear the restart fence.
17. seals a genuine terminal journal digest/status into the separately owned
    run marker and refuses uninstall while any transaction is unfinished,
    invalid or unsealed; the seal is written only after recomputed live fixture
    hashes exactly match the journal's terminal hashes; journal and marker
    updates fsync their file before rename and their containing directory after
    rename;
18. fsyncs prepared files, backup entries, fixture replacements, rollback
    replacements/unlinks, cleanup unlinks, and created-directory changes before
    durably advancing the transaction journal;
19. durably publishes every newly created state/run-directory ancestor and the
    initial owned run marker before copyback can begin, so a persisted fixture
    replacement cannot outlive the discoverable restart-fence directory solely
    because that directory entry was never synced.

Profile install/update also rejects a symlink or non-regular destination and
uses the same no-follow directory-handle replacement pattern. A path-label or
filesystem-identity conflict is a failed operation, never permission to follow
the replacement target.

## Important limit

Codex permission profiles constrain local command execution. They are not a
global policy for browser use, connectors, MCP servers, computer use, cloud
execution, or the Codex service connection. Those surfaces have separate
controls. The launcher disables the locally discoverable surfaces it knows
about, but a managed or future integration may not be visible to it.

For that reason, this beta must not receive credentials, `.env` files, private
keys, customer/payment/health data, private journals, or production access.
Use the bundled synthetic example first.

## A/B baseline

The ordinary arm is intentionally less restricted in what repository context
it can inspect, but both arms use the same no-network local permission profile
and run only inside generated fixtures containing explicitly declared safe
files. `compare` never copies either arm back to the source fixture.

Before either arm starts, a model-free preflight rejects legacy sandbox keys,
requires the reviewed profile to parse hosted web search as `disabled`,
and probes that the effective profile permits workspace writes while denying
outside reads, outside writes, network use, and an unlisted environment-secret
sentinel. The model rollout must then independently show the named permission
profile and exact staging working directory. Missing proof makes the run fail.

## Concurrency and recovery

A per-fixture lock prevents two launcher operations from writing the same
source concurrently. A crash may leave a stale lock in
`~/.codex-fast-lane/locks/`. Confirm that no process is still running before
removing that one lock directory manually.

Multi-file copyback is an application-level prepare/commit/rollback protocol,
not a filesystem-wide atomic transaction. Ordinary replacement failures are
rolled back and journaled; power loss or storage failure during commit may
still require manual recovery from the `.fast-lane-*.bak` file named by the
transaction evidence. Rollback failures deliberately preserve those backup
artifacts. Treat any `ROLLBACK_FAILED` receipt as non-actionable.

## Reporting a vulnerability

Open a GitHub issue only for non-sensitive reproduction details. Do not attach
credentials, private repositories, rollout logs, or personal data. For a
private report, use GitHub's private vulnerability reporting if enabled.
