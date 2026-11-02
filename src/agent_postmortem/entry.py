"""The console-script entry point (docs/DESIGN.md 4.14.5, 0.4).

`hook post-tool` and `hook prompt-submit` run on every tool call and every prompt of a session,
so they start here without importing the CLI (cli.py's imports alone take about 70 ms): the gate
in hook_gate.py decides from a few stats whether anything new can have happened. `hook
capabilities` too, since run.sh probes a CLI on PATH with it before each of those calls (7.2
step 2; 87 ms through the CLI). Every other command goes to cli.main unchanged.
"""

from __future__ import annotations

import json
import sys

_FAST_HOOKS = ("post-tool", "prompt-submit")


def capabilities() -> int:
    """`hook capabilities` (7.2): one static object, the protocol and the package version."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    from . import __version__  # the single source of truth (7.1's D1), no metadata import

    print(json.dumps({"protocol": 1, "version": __version__}))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "hook" and args[1] in _FAST_HOOKS:
        from . import hook_gate

        return hook_gate.run(args[1])
    if args == ["hook", "capabilities"]:
        return capabilities()
    from .cli import main as cli_main

    return cli_main(args)
