# Repair retry delay indexing

Fix `retry_delay()` so the first retry uses `base_seconds`, with later retries
doubling until the cap is reached.

## Required behavior

- `attempt` is one-based.
- attempt 1 returns `base_seconds`.
- attempt 2 returns `base_seconds * 2`.
- the result never exceeds `cap_seconds`.
- preserve the existing validation and public function signature.
- do not add dependencies or modify tests.

## Exact verification

`python3 -m unittest -v`
