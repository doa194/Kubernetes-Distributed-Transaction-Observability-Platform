"""Small, consistent console output for the CLIs.

Plain ASCII markers are used because Windows consoles do not always render symbols.
"""

from __future__ import annotations

import sys


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def info(message: str) -> None:
    print(f"    {message}", flush=True)


def ok(message: str) -> None:
    print(f"[ OK ] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


def fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr, flush=True)
