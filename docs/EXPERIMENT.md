# Originating experiment

Date: 2026-09-11

The first generalization canary compared an ordinary repository run with the
isolated task-local runner on two locally authored synthetic repairs.

| Case | Model / effort | Ordinary input | Fast input | Input change | Ordinary tools | Fast tools | Elapsed change |
|---|---|---:|---:|---:|---:|---:|---:|
| Python retry | gpt-5.6-sol / medium | 118,614 | 90,071 | -24.06% | 5 | 4 | -35.75% |
| Node path prefix | gpt-6-astra / high | 107,324 | 65,176 | -39.27% | 7 | 5 | -42.58% |

All four arms:

- returned model exit code `0`;
- passed the same exact seven-test verifier;
- changed only the declared implementation file;
- introduced no unexpected changed file.

Both fast arms also produced runtime evidence for the requested named
permission profile and isolated working directory before allowlisted copyback.

## What this does not prove

- The sample contains only two synthetic tasks.
- The ordinary arm ran first in both cases, so order and cache effects were not
  counterbalanced.
- Different models were used across the two cases.
- The experiment did not measure ChatGPT Pro quota behavior.
- The observed percentages are not an average, forecast, or guarantee for
  another user.

The public `compare` command therefore reports a single pair as
`DIRECTIONAL_ONE_PAIR` and supports reversing the arm order.

## Public-runner self-test

The packaged runner was then tested end-to-end on the bundled Python fixture
with `gpt-5.6-luna / low`, using `fast-first` order.

| Arm | Verifier | Changed files | Input | Tool calls | Elapsed |
|---|---|---|---:|---:|---:|
| Ordinary | 7/7 PASS | `src/retry_policy.py` only | 110,524 | 6 | 45.148s |
| Fast | 7/7 PASS | `src/retry_policy.py` only | 65,110 | 3 | 20.052s |

The allowed output was byte-identical between arms, the original fixture was
unchanged, and the result was `HELPED / DIRECTIONAL_ONE_PAIR`. In this one
pair, fast used 41.09% fewer input tokens and 55.59% less elapsed time. These
percentages describe only this recorded pair; they are not an average, causal
estimate, quota forecast, or promise to another tester.

Before that successful pair, an MCP override key was emitted with invalid TOML
syntax. Preflight rejected it before either model arm started and returned
`INVALID_COMPARISON`. The key writer was fixed, configured MCP servers were
read back as disabled, and only then was the A/B self-test rerun. The retained
failed receipt is evidence that an unproven boundary stops rather than silently
widening access.
