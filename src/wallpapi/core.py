"""The Core service: the interface between the UI and everything else, with SQLite as an in-process detail."""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from wallpapi import decisions, storage, workflows
from wallpapi import settings as settings_module
from wallpapi.allocation import ScoredWallpaper as ScoredWallpaper
from wallpapi.background import BackgroundLoop
from wallpapi.batches import Batch as Batch
from wallpapi.batches import BatchUnavailable as BatchUnavailable
from wallpapi.batches import SubmissionRefused as SubmissionRefused
from wallpapi.batches import Submitted as Submitted
from wallpapi.clock import Clock
from wallpapi.compose import background_loops, compose
from wallpapi.decisions import ResolvedVerdict
from wallpapi.library import FavouriteDownload, LibraryReconciliation, LibraryWriter
from wallpapi.model import DecisionEntry, Mix, Verdict, Wallpaper
from wallpapi.pool import wallpaper_from_row
from wallpapi.rng import SeededRandom

# Re-exported (`X as X`) for the tests that still import them from here, until #64 and #68 move them.
from wallpapi.settings import EXPLORE_MIX as EXPLORE_MIX
from wallpapi.settings import MAX_BATCH_SIZE as MAX_BATCH_SIZE
from wallpapi.settings import REFINE_MIX as REFINE_MIX
from wallpapi.settings import SUPERSEDED_POOL_TARGET_SIZE as SUPERSEDED_POOL_TARGET_SIZE
from wallpapi.settings import SUPERSEDED_SIMILARITY_RADIUS as SUPERSEDED_SIMILARITY_RADIUS
from wallpapi.settings import MixListing, Settings, SettingsRefused
from wallpapi.similarity import Embeddings
from wallpapi.wallhaven import Wallhaven
from wallpapi.workflows import HistoryRefused as HistoryRefused


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One **Wallpaper**'s line in **History**, not one entry: its resolved **Verdict** and its latest
    timestamp.
    """

    wallpaper: Wallpaper
    resolved: ResolvedVerdict
    latest_at: dt.datetime


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of **History**. `total` counts every matching row, so the page can say paging is happening."""

    rows: tuple[HistoryRow, ...]
    page: int
    pages: int
    total: int
    verdict: Verdict | None

    @property
    def previous_page(self) -> int | None:
        return self.page - 1 if self.page > 1 else None

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.page < self.pages else None


class CoreService:
    def __init__(
        self,
        *,
        db_path: Path,
        thumbnail_dir: Path,
        wallhaven: Wallhaven,
        library: LibraryWriter,
        similarity: Embeddings,
        random_source: SeededRandom,
        refill_random_source: SeededRandom,
        clock: Clock,
    ) -> None:
        self.modules = compose(
            db_path=db_path,
            thumbnail_dir=thumbnail_dir,
            wallhaven=wallhaven,
            library_writer=library,
            similarity=similarity,
            random_source=random_source,
            refill_random_source=refill_random_source,
            clock=clock,
        )
        """Transitional: what `compose` built, until the tests stop reaching for this class."""
        self._similarity = similarity
        self._clock = clock
        self._connect = self.modules.connect
        self.refill = self.modules.refill
        self.batches = self.modules.batches
        self.library = self.modules.library
        self.thumbnails = self.modules.thumbnails

    # -- storage ---------------------------------------------------------------------------------------

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        with storage.write(self._connect()) as connection:
            yield connection

    # -- settings --------------------------------------------------------------------------------------
    # Delegations to the settings module, kept until the callers move to it (#64, #68).

    def get_settings(self) -> Settings:
        """Everything configurable; see `settings.get`."""
        return settings_module.get(self._connect())

    def update_settings(self, **fields: object) -> Settings | SettingsRefused:
        """Validate and store the settings named, then prune the **Pool** to the **Filters**, in one write
        transaction; see `settings.update`.
        """
        return workflows.save_settings(self.modules, **fields)

    # -- mixes -----------------------------------------------------------------------------------------

    def list_mixes(self) -> tuple[Mix, ...]:
        """Every stored **Mix**, by name; see `settings.list_mixes`."""
        return tuple(listed.mix for listed in self.mix_listings())

    def mix_listings(self) -> tuple[MixListing, ...]:
        """Every stored **Mix**, by name, each saying whether it may be deleted."""
        return settings_module.list_mixes(self._connect())

    def active_mix(self) -> Mix:
        """The **Mix** the next **Batch** is built from; see `settings.active_mix`."""
        return settings_module.active_mix(self._connect())

    def save_mix(
        self, name: str, *, unknown: int | str, banger: int | str, dud: int | str
    ) -> Mix | SettingsRefused:
        """Store a **Mix** under that name; see `settings.save_mix`."""
        return workflows.save_mix(self.modules, name, unknown=unknown, banger=banger, dud=dud)

    def delete_mix(self, name: str) -> SettingsRefused | None:
        """Remove a **Mix**, or say why it stays; see `settings.delete_mix`."""
        return workflows.delete_mix(self.modules, name)

    # -- batches ---------------------------------------------------------------------------------------

    def get_next_batch(self) -> Batch | BatchUnavailable:
        """The live **Batch**, or a new one: `Batches.next`."""
        return self.batches.next(self._connect())

    def classify_pool(self) -> tuple[ScoredWallpaper, ...]:
        """The classified **Pool**: `Batches.classify`."""
        return self.batches.classify(self._connect())

    # -- the Similarity provider's own upkeep ------------------------------------------------------------

    def similarity_notice(self) -> str | None:
        """One line for the page when the **Similarity provider** is not at full strength.

        Handed the **Pool** less what the thumbnail downloader gave up on, so the line clears once everything
        that can be embedded has been.
        """
        return self._similarity.notice(self.thumbnails.obtainable(self._connect()))

    def similarity_step(self, stop_event: threading.Event) -> float:
        """One step of the **Similarity provider**'s upkeep, on its background thread only; seconds to the
        next.
        """
        return self._similarity.catch_up(self.thumbnails.directory, stop_event)

    def background_loops(self) -> tuple[BackgroundLoop, ...]:
        """The refill, the **Similarity provider**'s upkeep and the thumbnail downloader, in the order the
        lifespan starts them; it stops them in reverse.
        """
        return background_loops(self.modules)

    def set_draft_verdict(
        self, batch_id: str, wallpaper_id: str, verdict: Verdict | None
    ) -> Batch | SubmissionRefused:
        """Mark one tile, or clear it: `batches.set_draft` in its own transaction."""
        return workflows.set_draft(self.modules, batch_id, wallpaper_id, verdict)

    def set_all_draft_verdicts(self, batch_id: str, verdict: Verdict | None) -> Batch | SubmissionRefused:
        """Select-all or select-none: `batches.set_all_drafts` in its own transaction."""
        return workflows.set_all_drafts(self.modules, batch_id, verdict)

    def submit_batch(self, batch_id: str) -> Submitted | SubmissionRefused:
        """Submit the **Batch** in one transaction, then reconcile the **Library** and evict thumbnails."""
        return workflows.submit(self.modules, batch_id)

    # -- library ---------------------------------------------------------------------------------------

    def reconcile_library(self) -> LibraryReconciliation:
        """Make the **Library** folder agree with the **Decision log**: `Library.reconcile`."""
        return self.library.reconcile(self._connect(), self.get_settings().library_path)

    def download_favourites(self) -> FavouriteDownload:
        """Write a **Library** file for every **Favourite** without one: `Library.download_favourites`."""
        return workflows.download_favourites(self.modules)

    # -- thumbnails ------------------------------------------------------------------------------------

    def get_thumbnail(self, wallpaper_id: str) -> Path | None:
        """The cached thumbnail for a tile, fetched if missing: `Thumbnails.get`."""
        return self.thumbnails.get(self._connect(), wallpaper_id)

    # -- verdict resolution and history: forwarding to `decisions` until the web layer calls it directly ----

    def resolve_verdicts(self, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
        """**Verdict resolution** for many **Wallpapers** at once: `decisions.resolve`."""
        return decisions.resolve(self._connect(), wallpaper_ids)

    def edit_verdict(self, wallpaper_id: str, verdict: Verdict) -> HistoryRefused | None:
        """Change a **Wallpaper**'s **Verdict** from **History** by appending an entry with `batch_id` `NULL`.

        An **Ignore** is how **History** withdraws a **Verdict**, un-**Banning** included. An unknown
        **Wallpaper** is refused rather than left to the foreign key. The **Library** is reconciled
        afterwards, outside the transaction.
        """
        return workflows.edit_verdict(self.modules, wallpaper_id, verdict)

    def list_history_rows(self, *, verdict: Verdict | None = None, page: int = 1) -> HistoryPage:
        """One page of **History**: `decisions.history`, with each line's **Wallpaper**."""
        listing = decisions.history(self._connect(), verdict, page)
        return HistoryPage(
            rows=self._history_rows_from(listing.entries),
            page=listing.page,
            pages=listing.pages,
            total=listing.total,
            verdict=verdict,
        )

    def get_history_row(self, wallpaper_id: str) -> HistoryRow | None:
        """One **Wallpaper**'s **History** row, or `None`, for an edit to swap in: what it resolves to, and
        its latest entry's timestamp, which is the deciding one.
        """
        connection = self._connect()
        logged = decisions.entries(connection, wallpaper_id=wallpaper_id)
        if not logged:
            return None
        line = decisions.HistoryEntry(
            wallpaper_id=wallpaper_id,
            resolved=decisions.resolve(connection, [wallpaper_id])[wallpaper_id],
            decided_at=logged[-1].recorded_at,
        )
        return self._history_rows_from([line])[0]

    def _history_rows_from(self, lines: Sequence[decisions.HistoryEntry]) -> tuple[HistoryRow, ...]:
        """Join each line's **Wallpaper** on, the page's in one query."""
        placeholders = ",".join("?" * len(lines))
        rows = self._connect().execute(
            f"SELECT * FROM wallpapers WHERE id IN ({placeholders})", [line.wallpaper_id for line in lines]
        )
        wallpapers = {str(row["id"]): wallpaper_from_row(row) for row in rows}
        return tuple(
            HistoryRow(
                wallpaper=wallpapers[line.wallpaper_id], resolved=line.resolved, latest_at=line.decided_at
            )
            for line in lines
        )

    def list_history(
        self, *, batch_id: str | None = None, wallpaper_id: str | None = None
    ) -> list[DecisionEntry]:
        """The **Decision log**'s raw entries in sequence order: `decisions.entries`."""
        return decisions.entries(self._connect(), batch_id=batch_id, wallpaper_id=wallpaper_id)
