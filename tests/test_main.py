"""The entry point's commands: `reset-pool`, and serving when none is named.

Uses `make_harness`, because its subject is an adapter over a workflow, as the web tests are: `main` is handed
the harness's modules through its builder and a stand-in for serving, so nothing boots, fetches or listens.
What the **Pool reset** removes is `test_pool.py`'s; this is that the command runs it and says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, live, make_harness
from tests.fakes import catalogue_of
from wallpapi import batches, pool, workflows
from wallpapi.compose import Modules
from wallpapi.main import main


@pytest.fixture
def stocked(db_path: Path) -> Harness:
    """Six in the **Pool**, each thumbnail cached, and nothing decided."""
    harness = make_harness(db_path, catalogue=catalogue_of(6))
    workflows.save_settings(harness.modules, batch_size=2)
    for w in catalogue_of(6):
        harness.modules.thumbnails.get(harness.connect(), w.id)
    return harness


def never_serve() -> None:
    raise AssertionError("a command must not serve")


def test_reset_pool_resets_through_the_modules_it_builds_and_says_what_it_removed(
    stocked: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    live(stocked)

    main(["reset-pool"], build=lambda: stocked.modules, serve=never_serve)

    assert pool.members(stocked.connect()) == []
    assert batches.showing(stocked.connect()) == set()
    assert capsys.readouterr().out == (
        "Pool members removed: 6\nLive Batch discarded: yes\nThumbnails evicted: 6\n"
    )


def test_reset_pool_says_when_there_was_no_live_batch(
    stocked: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["reset-pool"], build=lambda: stocked.modules, serve=never_serve)

    assert "Live Batch discarded: no\n" in capsys.readouterr().out


def test_no_command_serves_and_builds_nothing() -> None:
    served: list[bool] = []

    def never_build() -> Modules:
        raise AssertionError("serving builds its own app")

    main([], build=never_build, serve=lambda: served.append(True))

    assert served == [True]
