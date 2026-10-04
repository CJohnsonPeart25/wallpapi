"""Every write the page makes, as one transaction over the modules and what follows its commit.

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

from wallpapi import batches, decisions, pool, settings, storage, thumbnails
from wallpapi.batches import Batch, SubmissionRefused, Submitted
from wallpapi.compose import Modules
from wallpapi.decisions import HistoryEntry, HistoryRefused
from wallpapi.library import FavouriteDownload
from wallpapi.model import Mix, Verdict
from wallpapi.settings import Settings, SettingsRefused


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


def submit(modules: Modules, batch_id: str) -> Submitted | SubmissionRefused:
    """Submit the **Batch**, then reconcile the **Library** and evict thumbnails.

    The tail is outside the transaction: a download is a network call, and nothing the **Library** does may
    roll the **Decision log** back. Eviction comes before the next **Batch** is drawn, so nothing about to be
    drawn is counted as evictable.
    """
    connection = modules.connect()
    with storage.write(connection) as write:
        submitted = modules.batches.submit(write, batch_id)
    if isinstance(submitted, Submitted):
        current = settings.get(connection)
        modules.library.reconcile(connection, current.library_path)
        modules.thumbnails.evict(connection, thumbnails.cap_bytes(current))
    return submitted


def edit_verdict(modules: Modules, wallpaper_id: str, verdict: Verdict) -> HistoryEntry | HistoryRefused:
    """Change a **Wallpaper**'s **Verdict** from **History**, then reconcile the **Library**. An **Ignore** is
    how **History** withdraws a **Verdict**. Returns the line **History** now shows.
    """
    connection = modules.connect()
    with storage.write(connection) as write:
        edited = decisions.edit(write, wallpaper_id, verdict, at=modules.clock.now())
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


def download_favourites(modules: Modules) -> FavouriteDownload:
    """Write a **Library** file for every **Favourite** without one. The **Library** writes each file's
    record in its own short transaction (ADR 0006), so this opens none.
    """
    connection = modules.connect()
    return modules.library.download_favourites(connection, settings.get(connection).library_path)
