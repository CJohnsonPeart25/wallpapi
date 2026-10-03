"""Harness for driving the Core service the way the UI does, and the guard that keeps tests off the network.

Every test enters through the Core service with fakes behind it. The only reach past the seam is the raw
connection the migration and legacy-Clearance tests need, because nothing the seam offers can produce an
older database's rows or a Clearance any more.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import socket
import sqlite3
import subprocess
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from tests.fakes import (
    THUMBNAIL_BYTES,
    FakeClock,
    FakeLibraryWriter,
    FakeSimilarityProvider,
    FakeWallhavenClient,
    catalogue_of,
)
from wallpapi import storage
from wallpapi.core import Batch, CoreService
from wallpapi.library import LibraryWriter
from wallpapi.model import Clearance, Verdict, Wallpaper
from wallpapi.rng import SeededRandom
from wallpapi.web.app import create_app

FIXED_NOW = dt.datetime(2026, 9, 24, 11, 30, 0, tzinfo=dt.UTC)

SOURCE = Path(__file__).resolve().parent.parent / "src" / "wallpapi"
"""The package's source, for the tests that read it rather than run it."""

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
"""Allowed through the socket guard: every `TestClient` on Windows makes one loopback `connect` for
asyncio's self-pipe, so a guard that refused it would refuse the whole web suite."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Refuse every connection and name lookup that is not loopback, and fail the test at teardown if one
    was tried. Recorded as well as refused, because the background loops catch every error: an attempt
    made on the refill thread would otherwise pass silently."""
    attempts: list[str] = []
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def refuse(address: object) -> None:
        described = repr(address)
        host = address[0] if isinstance(address, tuple) else address  # pyright: ignore[reportUnknownVariableType]
        if isinstance(host, str) and host not in LOOPBACK:
            attempts.append(described)
            raise OSError(f"tests do not touch the network: {described}")

    def guarded_connect(self: socket.socket, address: object) -> None:
        refuse(address)
        connect(self, address)  # pyright: ignore[reportArgumentType]

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        refuse(address)
        return connect_ex(self, address)  # pyright: ignore[reportArgumentType]

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        refuse((host,))
        return getaddrinfo(host, *args, **kwargs)  # pyright: ignore[reportArgumentType, reportCallIssue]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield attempts
    assert not attempts, f"a test tried to reach the network: {attempts}"


@dataclass
class Harness:
    """A Core service plus the fakes behind it, so tests can assert on both sides of the seam."""

    core: CoreService
    wallhaven: FakeWallhavenClient
    library: FakeLibraryWriter
    """The fake the Core service writes through, unless `make_harness` was handed a real writer."""
    similarity: FakeSimilarityProvider
    clock: FakeClock

    def fill_pool(self, steps: int = 1) -> None:
        """Run the refill by hand, `steps` **API calls** worth. Never the thread."""
        for _ in range(steps):
            self.core.refill_step()


def make_harness(
    db_path: Path,
    *,
    catalogue: Sequence[Wallpaper] | None = None,
    now: dt.datetime = FIXED_NOW,
    seed: int = 1,
    search_seed: str | None = None,
    page_size: int = 24,
    thumbnail_bytes: bytes = THUMBNAIL_BYTES,
    fail_from_call: int | None = None,
    rate_limited_calls: int = 0,
    retry_after: float | None = None,
    like_results: Mapping[str, Sequence[Wallpaper]] | None = None,
    fill_pool: int = 1,
    similarities: dict[tuple[str, str], float] | None = None,
    similarity_notice: str | None = None,
    catch_up_waits: Sequence[float] = (),
    vectors: Mapping[str, NDArray[np.float32]] | None = None,
    library: LibraryWriter | None = None,
) -> Harness:
    """Build a Core service over `db_path`, with `fill_pool` refill steps already run (one page each).

    Safe to call twice on one path: that is how a restart is tested. `similarities` is
    `{(pool id, decided id): value}` for the fake provider, a **Wallpaper** against itself 1.0 and
    everything unnamed 0.0. `library` replaces the fake writer the Core service gets, for the tests of the
    real one.
    """
    wallhaven = FakeWallhavenClient(
        catalogue_of(24) if catalogue is None else catalogue,
        seed=search_seed,
        page_size=page_size,
        thumbnail_bytes=thumbnail_bytes,
        fail_from_call=fail_from_call,
        rate_limited_calls=rate_limited_calls,
        retry_after=retry_after,
        like_results=like_results,
    )
    fake_library = FakeLibraryWriter()
    similarity = FakeSimilarityProvider(
        similarities, notice=similarity_notice, catch_up_waits=catch_up_waits, vectors=vectors
    )
    clock = FakeClock(now)
    core = CoreService(
        db_path=db_path,
        wallhaven=wallhaven,
        library=fake_library if library is None else library,
        similarity=similarity,
        random_source=SeededRandom(seed),
        clock=clock,
    )
    harness = Harness(
        core=core, wallhaven=wallhaven, library=fake_library, similarity=similarity, clock=clock
    )
    harness.fill_pool(fill_pool)
    return harness


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wallpapi.db"


@pytest.fixture
def harness(db_path: Path) -> Harness:
    return make_harness(db_path)


@contextmanager
def serving(harness: Harness) -> Generator[TestClient]:
    """The app over `harness`, lifespan and all, with no background threads."""
    with TestClient(create_app(harness.core)) as client:
        yield client


@pytest.fixture
def web(harness: Harness) -> Iterator[tuple[Harness, TestClient]]:
    """The default harness and a client for the app over it."""
    with serving(harness) as client:
        yield harness, client


BATCH_ID = re.compile(r'name="batch_id" value="([0-9a-f]+)"')


def batch_id_of(body: str) -> str:
    match = BATCH_ID.search(body)
    assert match is not None, "the page must carry the Batch ID it will submit"
    return match.group(1)


def favourite(harness: Harness, *wallpaper_ids: str) -> None:
    """Mint a **Batch**, mark these **Favourite** and submit it. They must all be in that **Batch**."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = {w.id for w in batch.wallpapers}
    assert set(wallpaper_ids) <= shown, f"{set(wallpaper_ids) - shown} is not in the batch to judge"
    for wallpaper_id in wallpaper_ids:
        harness.core.set_draft_verdict(batch.id, wallpaper_id, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)


def favourite_the_whole_batch(harness: Harness, verdict: Verdict = Verdict.FAVOURITE) -> Batch:
    """Mark every **Wallpaper** on the live **Batch** and submit it, returning the **Batch** submitted."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, verdict)
    harness.core.submit_batch(batch.id)
    return batch


def submit_with(harness: Harness, marks: Mapping[str, Verdict | None]) -> Batch:
    """Draft `marks` against the live **Batch** and submit it. A tile left out keeps what the **Batch**
    was minted with, and `None` unmarks it."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    for wallpaper_id, verdict in marks.items():
        harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)
    return batch


def judge(harness: Harness, **marks: Verdict) -> None:
    """Give each named **Wallpaper** a **Verdict** from **History**, in the order given."""
    for wallpaper_id, verdict in marks.items():
        assert harness.core.edit_verdict(wallpaper_id, verdict) is None


def library_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """Make `link` a symlink to `target`, or skip: Windows needs a privilege tests cannot assume."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as unavailable:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks are not available here: {unavailable}")


def library_junction(link: Path, target: Path) -> None:
    """Make `link` a Windows directory junction to `target`, or skip.

    The symlink tests skip on an ordinary Windows account; a junction needs no privilege, is a reparse point
    just the same and `Path.resolve` follows it, so it keeps the escape case tested on the one platform
    wallpapi runs on.
    """
    if os.name != "nt":  # pragma: no cover - platform dependent
        pytest.skip("junctions are a Windows thing; the symlink tests carry this elsewhere")
    link.parent.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0:  # pragma: no cover - platform dependent
        pytest.skip(f"junctions are not available here: {made.stderr.decode(errors='replace').strip()}")


@contextmanager
def raw_connection(db_path: Path) -> Generator[sqlite3.Connection]:
    connection = storage.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def write_old_pool(db_path: Path, wallpapers: Sequence[Wallpaper]) -> None:
    """Put each in `wallpapers` and the **Pool**, as an older database whose migrations stopped early holds
    them."""
    with raw_connection(db_path) as connection, storage.write(connection) as write:
        for w in wallpapers:
            write.execute(
                "INSERT INTO wallpapers (id, width, height, ratio, category, purity, favourites, colours, "
                "thumbnail_url, full_url, page_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    w.id,
                    w.width,
                    w.height,
                    w.ratio,
                    w.category,
                    w.purity,
                    w.favourites,
                    ",".join(w.colours),
                    w.thumbnail_url,
                    w.full_url,
                    w.page_url,
                ),
            )
            write.execute(
                "INSERT INTO pool (wallpaper_id, fetched_at, source) VALUES (?, ?, 'random')",
                (w.id, FIXED_NOW.isoformat()),
            )


def write_log_entries(db_path: Path, entries: Mapping[str, Verdict | Clearance]) -> None:
    """Append one **History**-style entry per **Wallpaper**, behind the seam."""
    with raw_connection(db_path) as connection:
        connection.executemany(
            "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)",
            [(w, entry.value, FIXED_NOW.isoformat()) for w, entry in entries.items()],
        )


def write_legacy_clearance(db_path: Path, *wallpaper_ids: str) -> None:
    """Append a **Clearance** for each, as a database from before ADR 0015 may hold."""
    write_log_entries(db_path, dict.fromkeys(wallpaper_ids, Clearance.CLEARED))
