"""Run every automatic check in **Done** and exit non-zero if any fails.

    uv run python scripts/check.py

The pre-commit hook in `.githooks/` and CI both run this, so a commit and a PR are held to the same four
checks: `ruff check`, `ruff format --check`, `pyright` strict and `pytest`. Every check runs even after one
fails, each prints its own output, and the last lines say which passed and which failed. The exit code is
the verdict; read it rather than grepping the output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS = (
    ("ruff check", ("ruff", "check")),
    ("ruff format", ("ruff", "format", "--check")),
    ("pyright", ("pyright",)),
    ("pytest", ("pytest", "-q")),
)


def main() -> int:
    failed: list[str] = []
    for name, module_args in CHECKS:
        print(f"--- {name}", flush=True)
        if subprocess.run((sys.executable, "-m", *module_args), cwd=ROOT, check=False).returncode != 0:
            failed.append(name)
    print("---")
    for name, _ in CHECKS:
        print(f"{'FAIL' if name in failed else 'ok  '} {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
