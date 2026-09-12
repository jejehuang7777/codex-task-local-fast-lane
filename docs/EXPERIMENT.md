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

## Counter-ordered candidate recheck

After incorporating external review on measurement, staging/copyback, and path
identity, the pre-durability-patch `0.1.0b2` candidate was rerun twice with the same
`gpt-5.6-sol / medium` model setting and opposite arm orders.

| Order | Arm | Verifier | Changed files | Input | Cached input | Output | Reasoning output | Tools | Elapsed |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| fast-first | Ordinary | 7/7 PASS | declared file only | 287,569 | 252,800 | 3,888 | 1,339 | 13 | 111.748s |
| fast-first | Fast | 7/7 PASS | declared file only | 60,136 | 46,720 | 824 | 174 | 3 | 25.932s |
| ordinary-first | Ordinary | 7/7 PASS | declared file only | 211,376 | 187,008 | 3,648 | 1,018 | 12 | 90.119s |
| ordinary-first | Fast | 7/7 PASS | declared file only | 60,254 | 46,848 | 775 | 112 | 3 | 28.814s |

Both pairs returned `HELPED`; input changed by -79.09% and -71.49%, while
elapsed time changed by -76.79% and -68.03%. In both pairs, programmatic
verification and changed-file scope established equivalence, outputs also
happened to be byte-identical, and the original fixture remained unchanged.

The usage columns are kept separate exactly as reported by Codex. Cached input
is not added to input, and this document does not convert the counters into a
price claim because the receipt does not establish an account-specific billing
formula. These are still two executions of one synthetic task, not a general
accuracy or savings benchmark.
