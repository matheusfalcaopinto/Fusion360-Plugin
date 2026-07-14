"""Frozen Claude-side example; never imported by the benchmark framework."""


def run(_context: str) -> None:
    raise RuntimeError("example Claude artifact requires an injected executor")
