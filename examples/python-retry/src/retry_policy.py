"""Retry timing helpers."""


def retry_delay(attempt: int, base_seconds: int = 2, cap_seconds: int = 30) -> int:
    """Return the capped delay for a one-based retry attempt."""
    if not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if not isinstance(base_seconds, int) or base_seconds < 1:
        raise ValueError("base_seconds must be a positive integer")
    if not isinstance(cap_seconds, int) or cap_seconds < 1:
        raise ValueError("cap_seconds must be a positive integer")

    return min(base_seconds * (2**attempt), cap_seconds)
