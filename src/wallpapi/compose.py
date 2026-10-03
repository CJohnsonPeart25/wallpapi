"""The composition root: the one place that knows the whole graph. The entry point hands it the real
collaborators and the tests hand it fakes; the web layer, the workflows and the background loops get what it
builds.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from wallpapi import pool, similarity, storage, thumbnails
from wallpapi.background import BackgroundLoop
from wallpapi.batches import Batches
from wallpapi.clock import Clock
from wallpapi.library import Library, LibraryWriter
from wallpapi.rng import SeededRandom
from wallpapi.similarity import Embeddings
from wallpapi.wallhaven import Wallhaven


@dataclass(frozen=True, slots=True)
class Modules:
    """Every stateful module, built once over one database. The stateless ones are called by name."""

    connect: Callable[[], sqlite3.Connection]
    """This thread's connection to the database: the threadpool hands each request a different thread."""
    clock: Clock
    refill: pool.Refill
    batches: Batches
    library: Library
    thumbnails: thumbnails.Thumbnails
    similarity: Embeddings


def compose(
    *,
    db_path: Path,
    thumbnail_dir: Path,
    wallhaven: Wallhaven,
    library_writer: LibraryWriter,
    similarity: Embeddings,
    random_source: SeededRandom,
    refill_random_source: SeededRandom,
    clock: Clock,
) -> Modules:
    """Open and migrate the database, then build each module with its collaborators. The draw and the
    Refill get separate random sources, so a refill step never changes which **Batch** a seed draws.
    """
    connect = storage.ThreadConnections(db_path).get
    storage.migrate(connect())
    refill = pool.Refill(connect, wallhaven, clock, refill_random_source)
    return Modules(
        connect=connect,
        clock=clock,
        refill=refill,
        batches=Batches(similarity, refill.status, clock, random_source),
        library=Library(library_writer, clock),
        thumbnails=thumbnails.Thumbnails(thumbnail_dir, wallhaven, clock),
        similarity=similarity,
    )


def background_loops(modules: Modules) -> tuple[BackgroundLoop, ...]:
    """The refill, the **Similarity provider**'s upkeep and the thumbnail downloader, in the order the
    lifespan starts them; it stops them in reverse.
    """
    return (
        BackgroundLoop(
            partial(pool.refill_loop, modules.refill), name=pool.THREAD_NAME, join_timeout=pool.JOIN_TIMEOUT
        ),
        BackgroundLoop(
            partial(similarity.upkeep_loop, modules.similarity, modules.thumbnails.directory),
            name=similarity.THREAD_NAME,
            join_timeout=similarity.JOIN_TIMEOUT,
        ),
        BackgroundLoop(
            partial(thumbnails.download_loop, modules.thumbnails, modules.connect),
            name=thumbnails.THREAD_NAME,
            join_timeout=thumbnails.JOIN_TIMEOUT,
        ),
    )
