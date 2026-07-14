"""Frozen Codex-side example; never imported by the benchmark framework."""


def run(_context: str) -> None:
    raise RuntimeError("example Codex artifact requires an injected executor")
