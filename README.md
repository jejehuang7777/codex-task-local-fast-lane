# Codex Task-Local Fast Lane

A fail-closed beta for measuring and running **small, self-contained Codex
tasks** without dragging an unrelated repository history into every model turn.

It is not an “unlimited Codex” trick. It does not reuse ChatGPT web quota,
bypass billing, or guarantee token savings. It creates a declared local task
packet, stages only allowlisted files, requests a named no-network permission
profile, verifies the result, and refuses copyback when proof is incomplete.

## What the tester gets

`compare` runs the same synthetic task in two disposable copies:

- `ordinary`: the same local safety profile with declared safe baseline
  repository context;
- `fast`: only the task packet and required runtime files, under the dedicated
  permission profile.

It reports exact verifier status, changed-file scope, byte-identical outputs,
input tokens, tool calls, and elapsed time. The final result is one of:

- `HELPED`: both runs are equivalent and the fast arm used fewer input tokens;
- `NO_CLEAR_GAIN`: both runs are equivalent but this pair did not save input;
- `INVALID_COMPARISON`: output, verification, scope, or usage proof did not
  match. A lower token number never overrides this result.

One pair is directional evidence only. Run a second pair with the opposite
order before drawing a stronger conclusion.

## Requirements

- Codex CLI `0.138.0` or newer, signed in to your own account;
- Python `3.11` or newer;
- macOS or Linux for this first beta;
- a disposable, non-sensitive test fixture.

Codex permission profiles are currently beta. This repository may need updates
when Codex changes its profile schema. See OpenAI's official
[permissions guide](https://developers.openai.com/codex/permissions) and
[advanced configuration guide](https://developers.openai.com/codex/config-advanced).

## Five-minute safe trial

```bash
git clone https://github.com/jejehuang7777/codex-task-local-fast-lane.git
cd codex-task-local-fast-lane
python3 fastlane.py install-profile
python3 fastlane.py preflight examples/python-retry \
  --semantic-self-containment PASS \
  --packet-complete YES \
  --external-dependency NONE
python3 fastlane.py compare examples/python-retry \
  --semantic-self-containment PASS \
  --packet-complete YES \
  --external-dependency NONE \
  --model gpt-5.6-sol \
  --effort medium
```

The example contains a deliberate one-line bug and seven exact tests. `compare`
does not modify the example source. Evidence is stored under
`~/.codex-fast-lane/runs/`.

To reduce order effects, note the first result's `order`, then run the opposite:

```bash
python3 fastlane.py compare examples/python-retry \
  --semantic-self-containment PASS \
  --packet-complete YES \
  --external-dependency NONE \
  --order fast-first
```

## The sentence to give your Codex

> Use this repository's `compare` command on the included synthetic example.
> Keep the model and effort identical across both arms. Do not use my real
> repository, credentials, account data, deployment, or network. Explain the
> final `HELPED`, `NO_CLEAR_GAIN`, or `INVALID_COMPARISON` result in plain
> language and give me the `comparison.json` path.

## Eligible tasks

Use the fast lane only when every item is true:

- the fixture is disposable and contains no sensitive data;
- `TASK.md` contains the complete decision context;
- all readable and writable files are explicitly listed;
- an exact offline verification command exists;
- the task needs no network, secrets, account data, external memory, owner or
  business judgment, deployment, runtime, permission, or public-state change;
- the edit is local and reversible.

If any answer is unknown, do not force `PASS`; use an ordinary Codex task.

## Not eligible

Do not use this beta for production deployment, customer/payment/health data,
credentials, private journals, browser work, MCP workflows, database changes,
package installation, broad refactors, or tasks whose “right answer” depends on
unstated history.

## Using your own fixture

Copy `examples/python-retry/FAST_LANE.toml` and make every list exact. Put
additional **non-sensitive** context that an ordinary run may inspect in
`baseline_reads`; the fast arm will not receive those files. Existing write
targets must also appear in `allowed_reads`.

Run `preflight` before `compare` or `run`. `run` is the only command that can
copy verified allowlisted outputs back to its source fixture; `compare` never
does.

## Uninstall

To remove the beta profile and its generated staging/receipt directory through
one ownership-checked path, run:

```bash
python3 fastlane.py uninstall
```

The uninstaller removes only this exact shipped profile and run directories
carrying this beta's ownership marker. If it finds a modified profile, an
active/stale lock, or an unknown entry, it stops without guessing. It preserves
your base Codex config, login, repositories, credentials, and other files.

The profile path is:

```text
~/.codex/task-local-fast-lane.config.toml
```

Run evidence is kept separately in `~/.codex-fast-lane/` until `uninstall`.

## Evidence so far

The originating experiment observed lower input and elapsed time in two
synthetic cases, with all four arms passing the same seven-test verifier and
changing only the declared file. Those two cases do **not** estimate average
user savings. The packaged public runner also completed one directional
same-model A/B self-test; that result is a smoke test, not a savings claim.
Exact numbers, the first fail-closed invalid attempt, and limitations are in
[`docs/EXPERIMENT.md`](docs/EXPERIMENT.md).

## Security

Read [`SECURITY.md`](SECURITY.md) before testing on anything beyond the bundled
example. Permission profiles constrain local command execution; browser,
connectors, MCP, and other tool surfaces have separate controls. This launcher
disables the known local features and user-configured MCP servers it can name,
but the beta still requires non-sensitive fixtures.

## 中文一句話

這不是「無限流量」或偷接網頁版額度；它是把一個可丟棄、無隱私、有精確測試的小任務切成安全 A/B，讓程式自己判斷有沒有真的省，而不是靠感覺。
