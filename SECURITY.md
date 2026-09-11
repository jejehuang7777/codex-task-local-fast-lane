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
9. copies back only manifest allowlisted output files while holding a
   per-fixture lock.

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

## Reporting a vulnerability

Open a GitHub issue only for non-sensitive reproduction details. Do not attach
credentials, private repositories, rollout logs, or personal data. For a
private report, use GitHub's private vulnerability reporting if enabled.
