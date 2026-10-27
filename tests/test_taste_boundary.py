"""`taste/`'s boundary: code only, data in and data out, and nothing from the rest of wallpapi.

Read from the source with `ast`, as `test_threads.py` guards against `time.sleep`: parsed, not grepped, so a
docstring may name what the code must not do. Two rules:

- nothing under `taste/` imports a wallpapi module outside `taste/`, so it can leave as its own package;
- everything under `taste/` imports only from `PURE`, and never calls `open` or `__import__`, so it opens no
  file or connection, starts no thread and makes no network call. An allowlist, not a list of the modules
  that do those things: a module missing from a denylist would pass unseen.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import SOURCE

TASTE = SOURCE / "taste"
PACKAGE = "wallpapi"
TASTE_PACKAGE = f"{PACKAGE}.taste"

PURE = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "hashlib",
        "itertools",
        "json",
        "math",
        "numpy",
        "random",
        "statistics",
        "time",
        "typing",
    }
)
"""The top-level modules `taste/` may import besides itself: the standard library's pure parts and numpy.
`time` is the clock's; `time.sleep` is refused everywhere by `test_threads.py`. Adding one here is a claim
that it opens nothing, starts nothing and calls out to nothing."""

FORBIDDEN_CALLS = frozenset({"open", "__import__"})
"""Builtins that reach a file or a module without an import statement."""


def _package_of(path: Path) -> str:
    """The dotted package a source file's relative imports resolve against."""
    return ".".join(path.relative_to(SOURCE.parent).parts[:-1])


def imported(source: str, package: str) -> list[tuple[int, str]]:
    """Every name this source imports, absolute and with its line. `from a import b` is `a.b`, so `from
    wallpapi import pool` names the module it reaches; a relative import is resolved against `package`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            found += [(node.lineno, f"{base}.{alias.name}") for alias in node.names]
    return found


def outside_taste(source: str, package: str) -> list[int]:
    """Lines importing a wallpapi module that is not under `taste/`."""
    return [
        line
        for line, name in imported(source, package)
        if (name == PACKAGE or name.startswith(f"{PACKAGE}."))
        and not (name == TASTE_PACKAGE or name.startswith(f"{TASTE_PACKAGE}."))
    ]


def impure(source: str, package: str) -> list[int]:
    """Lines importing anything outside `PURE` and `taste/`, or calling a `FORBIDDEN_CALLS` builtin."""
    lines = [
        line
        for line, name in imported(source, package)
        if name.split(".")[0] not in PURE and not name.startswith(f"{TASTE_PACKAGE}.")
    ]
    lines += [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS
    ]
    return sorted(lines)


def _taste_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in sorted(TASTE.rglob("*.py"))}


def test_taste_is_a_folder_inside_wallpapi_with_source_to_guard() -> None:
    """An empty folder would pass every guard below."""
    assert (TASTE / "__init__.py").is_file()
    assert _package_of(TASTE / "__init__.py") == TASTE_PACKAGE


def test_nothing_under_taste_imports_the_rest_of_wallpapi() -> None:
    offenders = {
        path.relative_to(SOURCE).as_posix(): lines
        for path, source in _taste_sources().items()
        if (lines := outside_taste(source, _package_of(path)))
    }

    assert offenders == {}


def test_nothing_under_taste_opens_a_file_or_connection_starts_a_thread_or_calls_out() -> None:
    offenders = {
        path.relative_to(SOURCE).as_posix(): lines
        for path, source in _taste_sources().items()
        if (lines := impure(source, _package_of(path)))
    }

    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        "import wallpapi.pool",
        "import wallpapi",
        "import wallpapi.pool as pool",
        "from wallpapi import pool",
        "from wallpapi.pool import members",
        "from .. import pool",
        "from ..pool import members",
        "from ..web.app import create_app",
    ],
    ids=["dotted", "package", "renamed", "from package", "from module", "up", "up a module", "deep"],
)
def test_the_import_guard_sees_every_spelling(source: str) -> None:
    assert outside_taste(source, TASTE_PACKAGE) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "from wallpapi.taste.clock import Clock",
        "from wallpapi.taste import clock",
        "from wallpapi import taste",
        "from . import clock",
        "from .clock import Clock",
        "import numpy as np",
    ],
)
def test_the_import_guard_passes_taste_and_the_outside_world(source: str) -> None:
    assert outside_taste(source, TASTE_PACKAGE) == []


@pytest.mark.parametrize(
    "source",
    [
        "import sqlite3",
        "import threading",
        "from pathlib import Path",
        "import httpx2",
        "from urllib.request import urlopen",
        "import importlib",
        "open('x')",
        "__import__('socket')",
    ],
)
def test_the_purity_guard_sees_what_reaches_outside(source: str) -> None:
    assert impure(source, TASTE_PACKAGE) == [1]


def test_the_purity_guard_passes_pure_code() -> None:
    source = (
        "from __future__ import annotations\nimport datetime as dt\nimport numpy as np\n"
        "from numpy.typing import NDArray\nfrom .evidence import AbsoluteEvidence\n"
    )

    assert impure(source, TASTE_PACKAGE) == []
