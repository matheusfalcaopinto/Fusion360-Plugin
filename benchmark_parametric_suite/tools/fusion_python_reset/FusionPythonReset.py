"""One-shot diagnostic used to restore Fusion's embedded Python streams."""

import sys


def run(_context):
    # The native MCP script executor temporarily replaces these streams.  A
    # failed nested capture can leave a self-referential proxy installed and
    # make every later MCP script recurse in ``__getattr__``.  Running this as
    # a normal Fusion script restores the interpreter-owned streams without
    # changing the active document.
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
