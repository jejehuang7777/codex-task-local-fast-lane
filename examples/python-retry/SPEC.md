# Retry policy contract

`retry_delay(attempt, base_seconds=2, cap_seconds=30)` accepts positive integer
arguments and returns a capped exponential delay. `attempt` is one-based.
