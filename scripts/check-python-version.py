#!/usr/bin/env python3
"""Fail unless this interpreter is the Python version named on the command line.

CI's matrix once named 3.11, 3.12 and 3.13 while every job ran what uv found first, and nothing said
so. Each CI job and each scripts/ci-like.sh run calls this, so a job on the wrong Python fails.
"""

from __future__ import annotations

import platform
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check-python-version.py MAJOR.MINOR", file=sys.stderr)
        return 2
    running = "%d.%d" % sys.version_info[:2]
    if running != argv[1]:
        print(f"asked for Python {argv[1]}, running {platform.python_version()}", file=sys.stderr)
        return 1
    print(f"{platform.python_implementation()} {platform.python_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
