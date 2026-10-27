"""Every write the page or a command makes, as one transaction over the modules and what follows its commit.

A workflow opens the write, hands the handle to each module it calls, and runs what must stay outside the
transaction after it commits: reconciling the **Library**, then evicting thumbnails. Both report their
failures rather than raise, so nothing after a commit can undo or hide it. A workflow decides nothing about
the data: every refusal, filter and policy is a module's; a workflow orders the calls and passes results back.

A workflow owns every transaction that spans more than one module's tables or has a post-commit tail. A
module may open its own short write over only its own tables when nothing composes it into a larger
transaction: `Batches.next` minting a **Batch** (re-read under the lock, ADR 0002), `Refill.step` admitting a
page after its network call, `EmbeddingCache` in its own file, and the **Library**'s per-file records (ADR
0006).
"""

from __future__ import annotations

from dataclasses import dataclass

from wallpapi import batches, evidence_log, lineup, pool, settings, storage, thumbnails
from wallpapi.batches import Batch, SubmissionRefused, Submitted
from wallpapi.compose import Modules
from wallpapi.evidence_log import HistoryEntry, HistoryRefused, Theme
from wallpapi.library import FavouriteDownload
from wallpapi.lineup import Lineup, LineupRefused, Picked
from wallpapi.model import Mix, Verdict
from wallpapi.settings import Settings, SettingsRefused
from wallpapi.thumbnails import ThumbnailEviction


def set_draft(
    modules: Modules, batch_id: str, wallpaper_id: str, verdict: Verdict | None
) -> Batch | SubmissionRefused:
    """Mark one tile, or clear it with `None`."""
    with storage.write(modules.connect()) as write:
        return batches.set_draft(write, batch_id, wallpaper_id, verdict)


def set_all_drafts(modules: Modules, batch_id: str, verdict: Verdict | None) -> Batch | SubmissionRefused:
    """Select-all, or select-none with `None`."""
    with storage.write(modules.connect()) as write:
        return batches.set_all_drafts(write, batch_id, verdict)


def open_preview(modules: Modules, batch_id: str, wallpaper_id: str) -> Batch | SubmissionRefused:
    """Record on the **Draft Batch** that a tile's preview was opened."""
    with storage.write(modules.connect()) as write:
        return batches.open_preview(write, batch_id, wallpaper_id)


def submit(modules: Modules, batch_id: str, *, theme: Theme | None = None) -> Submitted | SubmissionRefused:
    """Submit the **Batch** with the page's `theme`, then reconcile the **Library** and evict thumbnails.

    The tail is outside the transaction: a download is a network call, and nothing the **Library** does may
    roll the **Evidence log** back. Eviction comes before the next **Batch** is drawn, so nothing about to be
    drawn is counted as evictable.
    """
    connection = modules.connect()
    with storage.write(connection) as write:
        submitted = modules.batches.submit(write, batch_id, theme=theme)
    if isinstance(submitted, Submitted):
        current = settings.get(connection)
        modules.library.reconcile(connection, current.library_path)
        modules.thumbnails.evict(connection, thumbnails.cap_bytes(current))
    return submitted


def skip(modules: Modules, batch_id: str, *, theme: Theme | None = None) -> SubmissionRefused | None:
    """Record a **Skip**, with the page's `theme`, and drop the live **Batch**, or say why not (ADR 0026).
    No tail: nothing left the **Pool** and nothing was decided, so there is nothing to reconcile or evict.
    """
    with storage.write(modules.connect()) as write:
        return modules.batches.skip(write, batch_id, theme=theme)


def star_lineup_tile(
    modules: Modules, lineup_id: str, wallpaper_id: str, *, starred: bool
) -> Lineup | LineupRefused:
    """Star one tile of the live **Line-up**, or take its star off. Held until the **Line-up** is answered,
    so nothing is decided yet and there is no tail."""
    with storage.write(modules.connect()) as write:
        return lineup.star(write, lineup_id, wallpaper_id, starred=starred)


def pick(
    modules: Modules, lineup_id: str, winner: str | None, *, theme: Theme | None = None
) -> Picked | LineupRefused:
    """Answer the live **Line-up** with `winner`, or "none of these" with `None`, with the page's `theme`;
    then, if a tile was starred, reconcile the **Library**, as a submission does.

    Only a starred answer changes a **Favourite**, so only it reconciles: an unstarred **Pick** makes no
    network call, and the next **Line-up** comes at once. No eviction: the only **Wallpapers** a **Pick**
    takes out of the **Pool** are its **Favourites**, whose thumbnails are kept for ever, and every other
    tile stays in the **Pool**.
    """
    connection = modules.connect()
    with storage.write(connection) as write:
        picked = modules.lineups.pick(write, lineup_id, winner, theme=theme)
    if isinstance(picked, Picked) and picked.starred:
        modules.library.reconcile(connection, settings.get(connection).library_path)
    return picked


def choose_lineup_size(modules: Modules, size: str) -> Settings | SettingsRefused:
    """Set the **Line-up** size, or say why not. It changes no **Filter**, so the **Pool** is left as it is,
    and the live **Line-up** keeps the size it was drawn with."""
    with storage.write(modules.connect()) as write:
        return settings.update(write, lineup_size=size)


def edit_verdict(modules: Modules, wallpaper_id: str, verdict: Verdict) -> HistoryEntry | HistoryRefused:
    """Change a **Wallpaper**'s **Verdict** from **History**, then reconcile the **Library**. An **Ignore** is
    how **History** withdraws a **Verdict**. Returns the line **History** now shows.
    """
    connection = modules.connect()
    with storage.write(connection) as write:
        edited = evidence_log.edit(write, wallpaper_id, verdict, clock=modules.clock)
    if isinstance(edited, HistoryEntry):
        modules.library.reconcile(connection, settings.get(connection).library_path)
    return edited


def save_settings(modules: Modules, **fields: object) -> Settings | SettingsRefused:
    """Store the settings named, then prune the **Pool** to the **Filters**, in one transaction."""
    with storage.write(modules.connect()) as write:
        updated = settings.update(write, **fields)
        if isinstance(updated, Settings):
            pool.prune(write, updated)
        return updated


def choose_mix(modules: Modules, name: str) -> Settings | SettingsRefused:
    """Make that **Mix** the active one, or say why not. A **Mix** switch changes no **Filter**, so the
    **Pool** is left as it is; the live **Batch** keeps the **Mix** it was built with.
    """
    with storage.write(modules.connect()) as write:
        return settings.update(write, active_mix=name)


def save_mix(
    modules: Modules, name: str, *, unknown: int | str, banger: int | str, dud: int | str
) -> Mix | SettingsRefused:
    """Store a **Mix** under that name, new or edited."""
    with storage.write(modules.connect()) as write:
        return settings.save_mix(write, name, unknown=unknown, banger=banger, dud=dud)


def delete_mix(modules: Modules, name: str) -> SettingsRefused | None:
    """Remove a **Mix**, or say why it stays."""
    with storage.write(modules.connect()) as write:
        return settings.delete_mix(write, name)


@dataclass(frozen=True, slots=True)
class PoolReset:
    """What a **Pool reset** removed: the **Pool** members, the unsubmitted **Batches**, the live
    **Line-up** and the thumbnails."""

    members: int
    batches_discarded: int
    lineups_discarded: int
    eviction: ThumbnailEviction


def reset_pool(modules: Modules) -> PoolReset:
    """Empty the **Pool** and discard the live **Batch** and **Line-up** in one transaction, then evict
    thumbnails (ADR 0025).

    The refill restocks to the **Pool target size** on its next look. Nothing is reconciled: no **Verdict**
    changes. Eviction comes after the commit, when what was purged is no longer awaited.
    """
    connection = modules.connect()
    with storage.write(connection) as write:
        members = pool.clear(write)
        discarded = batches.discard_live(write)
        lineups_discarded = lineup.discard_live(write)
    eviction = modules.thumbnails.evict(connection, thumbnails.cap_bytes(settings.get(connection)))
    return PoolReset(
        members=members, batches_discarded=discarded, lineups_discarded=lineups_discarded, eviction=eviction
    )


def download_favourites(modules: Modules) -> FavouriteDownload:
    """Write a **Library** file for every **Favourite** without one. The **Library** writes each file's
    record in its own short transaction (ADR 0006), so this opens none.
    """
    connection = modules.connect()
    return modules.library.download_favourites(connection, settings.get(connection).library_path)
