"""Harness for driving the Core service the way the UI does.

Every test enters through the Core service. Nothing here touches the network, and nothing reaches past the
seam into storage to check a result — if a behaviour isn't observable through the Core service, it isn't
asserted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fakes import (
    FakeClock,
    FakeLibraryWriter,
    FakeSimilarityProvider,
    FakeWallhavenClient,
    catalogue_of,
)
from wallpapi.core import CoreService
from wallpapi.model import Wallpaper
from wallpapi.rng import SeededRandom

FIXED_NOW = dt.datetime(2026, 9, 24, 11, 30, 0, tzinfo=dt.UTC)


@dataclass
class Harness:
    """A Core service plus the fakes behind it, so tests can assert on both sides of the seam."""

    core: CoreService
    wallhaven: FakeWallhavenClient
    library: FakeLibraryWriter
    similarity: FakeSimilarityProvider
    clock: FakeClock


def make_harness(
    db_path: Path,
    *,
    catalogue: Sequence[Wallpaper] | None = None,
    now: dt.datetime = FIXED_NOW,
    seed: int = 1,
    search_seed: str | None = None,
    page_size: int = 24,
    fail_from_call: int | None = None,
) -> Harness:
    """Build a Core service over `db_path`.

    Called twice with the same path to prove the Decision log survives a restart, so it must run migrations
    idempotently rather than assuming an empty database.
    """
    wallhaven = FakeWallhavenClient(
        catalogue_of(24) if catalogue is None else catalogue,
        seed=search_seed,
        page_size=page_size,
        fail_from_call=fail_from_call,
    )
    library = FakeLibraryWriter()
    similarity = FakeSimilarityProvider()
    clock = FakeClock(now)
    core = CoreService(
        db_path=db_path,
        wallhaven=wallhaven,
        library=library,
        similarity=similarity,
        random_source=SeededRandom(seed),
        clock=clock,
    )
    return Harness(core=core, wallhaven=wallhaven, library=library, similarity=similarity, clock=clock)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wallpapi.db"


@pytest.fixture
def harness(db_path: Path) -> Harness:
    return make_harness(db_path)
