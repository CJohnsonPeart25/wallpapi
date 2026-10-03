"""The three background threads: they start with the app, run their loop, and stop when told.

What the loops *decide* belongs to their modules (the pool's refill, the similarity upkeep, the Core
service's downloader), which the other tests drive by hand. What is left here is what a fake cannot stand in
for: a loop on another thread, and a shutdown that has to end it. Every wait is on an event a fake sets, never
a sleep, and fails the test rather than hanging.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SOURCE, Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi import compose, pool, similarity, thumbnails, workflows
from wallpapi.background import BackgroundLoop
from wallpapi.pool import JOIN_TIMEOUT
from wallpapi.similarity import CAUGHT_UP
from wallpapi.web.app import create_app


def _running(name: str) -> bool:
    return any(thread.name == name for thread in threading.enumerate())


def _loop_named(harness: Harness, name: str) -> BackgroundLoop:
    (loop,) = [loop for loop in compose.background_loops(harness.modules) if loop.name == name]
    return loop


def test_the_lifespan_starts_the_refill_and_stops_it_cleanly(db_path: Path) -> None:
    """The **Pool** is already at its target, so the loop's first act is a long cancellable wait: what is
    checked is that the thread starts, the wait is cancellable and the join returns."""
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=1)
    workflows.save_settings(harness.modules, pool_target_size=1)
    app = create_app(harness.modules, refill=True)

    with TestClient(app) as client:
        response = client.get("/batch")
        assert response.status_code == 200
        assert harness.modules.refill.status().running, "the lifespan should have started the thread"
        assert "Refill idle" in response.text

    assert not harness.modules.refill.status().running, "shutdown should have joined the thread"


def test_the_refill_thread_fills_the_pool_from_an_empty_start(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=0)
    workflows.save_settings(harness.modules, pool_target_size=1)
    app = create_app(harness.modules, refill=True)

    with TestClient(app) as client:
        client.get("/batch")
        assert harness.wallhaven.searched.wait(JOIN_TIMEOUT), "the thread should have searched"

    assert harness.modules.refill.status().pool_size == 24


def test_the_lifespan_starts_the_similarity_upkeep_without_a_request(db_path: Path) -> None:
    """The model is fetched off the request path, and shutdown is clean."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    workflows.save_settings(harness.modules, pool_target_size=1)
    app = create_app(harness.modules, refill=True)

    with TestClient(app):
        assert harness.model.asked.wait(JOIN_TIMEOUT), "the thread should have fetched the model"


def test_the_upkeep_embeds_the_thumbnail_cache(db_path: Path) -> None:
    """The images wallpapi holds are the **Thumbnail cache** (`thumbnails.py`); a **Wallpaper** with no
    thumbnail there is simply not embedded and falls back to the baseline."""
    harness = make_harness(db_path)
    harness.modules.thumbnails.directory.mkdir(parents=True, exist_ok=True)
    (harness.modules.thumbnails.directory / "wp0003.jpg").write_bytes(b"thumbnail")

    harness.modules.similarity.catch_up(harness.modules.thumbnails.directory, threading.Event())

    assert harness.embed.seen == ["wp0003"]


def test_a_backlog_is_worked_through_without_waiting(db_path: Path) -> None:
    """A step that leaves more to do asks to come straight back, so three thumbnails one at a time are
    embedded well inside `CAUGHT_UP`, the wait the loop settles into after."""
    harness = make_harness(db_path, embed_batch=1)
    harness.modules.thumbnails.directory.mkdir(parents=True, exist_ok=True)
    for name in ("wp0001", "wp0002", "wp0003"):
        (harness.modules.thumbnails.directory / f"{name}.jpg").write_bytes(b"thumbnail")
    upkeep = _loop_named(harness, similarity.THREAD_NAME)

    upkeep.start()
    try:
        assert harness.embed.wait_for(3, CAUGHT_UP / 2), "a 0.0 should be followed straight up"
    finally:
        upkeep.stop()

    assert harness.embed.batches == [1, 1, 1]


def test_the_lifespan_starts_the_downloader_and_it_fetches_on_its_own_thread(db_path: Path) -> None:
    """Thumbnail fetching for the **Pool** never runs on a request thread, inside `refill_loop` or on the
    similarity thread, and it starts through the `refill` flag."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    workflows.save_settings(harness.modules, pool_target_size=1)
    app = create_app(harness.modules, refill=True)

    with TestClient(app):
        assert harness.wallhaven.thumbnail_fetched.wait(JOIN_TIMEOUT), "the downloader should have fetched"
        assert _running(thumbnails.THREAD_NAME)

    assert harness.wallhaven.thumbnail_threads
    assert set(harness.wallhaven.thumbnail_threads) == {"wallpapi-thumbnails"}
    assert not _running(thumbnails.THREAD_NAME), "shutdown should have joined the downloader"


def test_without_the_refill_flag_no_downloader_starts(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    app = create_app(harness.modules)

    with TestClient(app) as client:
        client.get("/batch")
        assert not _running(thumbnails.THREAD_NAME)

    assert harness.wallhaven.thumbnail_fetches == []


def test_a_stop_during_a_fetch_ends_the_downloader_once_the_fetch_returns(db_path: Path) -> None:
    """The stop lands while a fetch is held in flight. When the fetch returns, as a real one must inside
    `REQUEST_TIMEOUT`, the loop goes back to its `stop_event` rather than on to the next **Pool** member."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    release = threading.Event()
    harness.wallhaven.hold_thumbnails = release
    downloader = _loop_named(harness, thumbnails.THREAD_NAME)
    downloader.start()
    assert harness.wallhaven.thumbnail_fetched.wait(JOIN_TIMEOUT), "a fetch should be in flight"
    (thread,) = [t for t in threading.enumerate() if t.name == thumbnails.THREAD_NAME]

    downloader.stop(timeout=0.0)
    release.set()
    thread.join(JOIN_TIMEOUT)

    assert not thread.is_alive(), "the loop should have ended inside the join timeout"
    assert len(harness.wallhaven.thumbnail_fetches) == 1


@pytest.mark.parametrize("name", [pool.THREAD_NAME, similarity.THREAD_NAME, thumbnails.THREAD_NAME])
def test_starting_and_stopping_twice_is_harmless(name: str, db_path: Path) -> None:
    """A stop without a start, and a second stop, must not raise: shutdown paths get run twice."""
    harness = make_harness(db_path, fill_pool=0)
    workflows.save_settings(harness.modules, pool_target_size=1)
    thread = _loop_named(harness, name)

    thread.stop()
    thread.start()
    thread.start()
    thread.stop()
    thread.stop()

    assert not _running(name)


def test_the_lifespan_starts_the_loops_in_order_and_stops_them_in_reverse(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    start, stop = BackgroundLoop.start, BackgroundLoop.stop

    def recording_start(self: BackgroundLoop) -> None:
        events.append(("start", self.name))
        start(self)

    def recording_stop(self: BackgroundLoop, timeout: float | None = None) -> None:
        events.append(("stop", self.name))
        stop(self, timeout)

    monkeypatch.setattr(BackgroundLoop, "start", recording_start)
    monkeypatch.setattr(BackgroundLoop, "stop", recording_stop)
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    workflows.save_settings(harness.modules, pool_target_size=1)

    with TestClient(create_app(harness.modules, refill=True)):
        pass

    assert events == [
        ("start", pool.THREAD_NAME),
        ("start", similarity.THREAD_NAME),
        ("start", thumbnails.THREAD_NAME),
        ("stop", thumbnails.THREAD_NAME),
        ("stop", similarity.THREAD_NAME),
        ("stop", pool.THREAD_NAME),
    ]


def test_a_background_loop_runs_on_its_named_thread_until_stopped() -> None:
    """The loop is handed the stop event; `stop` sets it and joins, and a second start or stop is harmless."""
    entered = threading.Event()
    ran_on: list[str] = []

    def loop(stop_event: threading.Event) -> None:
        ran_on.append(threading.current_thread().name)
        entered.set()
        stop_event.wait()

    background = BackgroundLoop(loop, name="wallpapi-test-loop", join_timeout=JOIN_TIMEOUT)
    background.start()
    background.start()
    assert entered.wait(JOIN_TIMEOUT), "the loop should have started"
    assert _running("wallpapi-test-loop"), "the loop should still be waiting on its stop event"

    background.stop()
    background.stop()

    assert ran_on == ["wallpapi-test-loop"]
    assert not _running("wallpapi-test-loop")


def _sleep_calls(path: Path) -> list[int]:
    """Lines calling `time.sleep`, however it was imported. Parsed, not grepped: a docstring may say it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    time_names = {"time"}
    sleep_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            time_names |= {alias.asname or alias.name for alias in node.names if alias.name == "time"}
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            sleep_names |= {alias.asname or alias.name for alias in node.names if alias.name == "sleep"}
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "sleep" and isinstance(func.value, ast.Name):
            if func.value.id in time_names:
                lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in sleep_names:
            lines.append(node.lineno)
    return lines


def test_no_source_file_calls_time_sleep() -> None:
    """Invariant 12: every wait is `stop_event.wait(n)`, so shutdown never hangs behind a sleep."""
    offenders = {
        str(path.relative_to(SOURCE)): lines
        for path in sorted(SOURCE.rglob("*.py"))
        if (lines := _sleep_calls(path))
    }

    assert offenders == {}


def test_the_sleep_guard_sees_every_spelling(tmp_path: Path) -> None:
    source = tmp_path / "sleepy.py"
    source.write_text(
        '"""Never `time.sleep(n)`."""\n'
        "import time\nimport time as t\nfrom time import sleep\nfrom time import sleep as nap\n"
        "time.sleep(1)\nt.sleep(1)\nsleep(1)\nnap(1)\n",
        encoding="utf-8",
    )

    assert _sleep_calls(source) == [6, 7, 8, 9]
