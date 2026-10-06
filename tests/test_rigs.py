"""The suite's own rule: a rule test builds its one module on a rig, never the composed harness.

Invariant 14 and ADR 0019. `make_harness` composes every module, so a rule test on it passes through modules
it is not about, and can go green for a reason that is not its own. It is for a workflow's transaction or
its post-commit tail, for the web tests and for the background threads; each file allowed it says why in its
docstring. Read from the source with `ast`, as `test_threads.py` guards against `time.sleep`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

ON_THE_HARNESS = frozenset(
    {
        "test_library.py",
        "test_pool.py",
        "test_threads.py",
        "test_thumbnails.py",
        "test_web_pages.py",
        "test_web_routes.py",
    }
)
"""The files allowed `make_harness`: the web and thread tests whole, and the workflow sections of the rest."""

HARNESS_NAMES = frozenset({"make_harness", "Harness"})
HARNESS_FIXTURES = frozenset({"harness", "web"})
"""The `conftest` fixtures that build a harness."""


def uses_the_harness(source: str) -> bool:
    """Whether this source names `make_harness` or `Harness`, or asks for a fixture that builds one, as a
    parameter or through `usefixtures`."""
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.alias(name=name) | ast.Name(id=name) | ast.Attribute(attr=name) if name in HARNESS_NAMES:
                return True
            case ast.FunctionDef(args=arguments) if any(
                argument.arg in HARNESS_FIXTURES for argument in arguments.args + arguments.kwonlyargs
            ):
                return True
            case ast.Call(func=ast.Attribute(attr="usefixtures"), args=fixtures) if any(
                isinstance(fixture, ast.Constant) and fixture.value in HARNESS_FIXTURES
                for fixture in fixtures
            ):
                return True
            case _:
                pass
    return False


def _test_files() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(TESTS.glob("test_*.py"))}


def test_only_workflow_web_and_thread_tests_use_the_harness() -> None:
    """Exactly the allowlist: a file outside it is a rule test that drifted back, and one on it that no longer
    needs the harness should leave it."""
    on_the_harness = {name for name, source in _test_files().items() if uses_the_harness(source)}

    assert on_the_harness == ON_THE_HARNESS


def test_each_file_on_the_harness_says_why_in_its_docstring() -> None:
    files = _test_files()

    silent = sorted(
        name
        for name in ON_THE_HARNESS
        if "make_harness" not in (ast.get_docstring(ast.parse(files[name])) or "")
    )

    assert silent == []


@pytest.mark.parametrize(
    "source",
    [
        "from tests.conftest import make_harness",
        "from tests.conftest import Harness",
        "from tests.conftest import make_harness as build",
        "import tests.conftest\ntests.conftest.make_harness",
        "def test_x(harness): ...",
        "def test_x(*, web): ...",
        "import pytest\n@pytest.mark.usefixtures('harness')\ndef test_x(): ...",
    ],
    ids=["imported", "the type", "renamed", "an attribute", "the fixture", "keyword-only", "usefixtures"],
)
def test_the_harness_guard_sees_every_spelling(source: str) -> None:
    assert uses_the_harness(source)


def test_the_harness_guard_passes_a_rig() -> None:
    assert not uses_the_harness(
        "from tests.conftest import BatchesRig, batches_rig\ndef test_x(rig: BatchesRig, memory): ..."
    )
