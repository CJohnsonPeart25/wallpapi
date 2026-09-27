"""Marking a whole **Batch** at once: select-all and select-none.

One post rewriting the whole **Draft Batch** in one transaction, not one per tile (invariant 6). At a
**Batch** size of 32 the per-tile version is 32 posts and 32 transactions for one click, and it can
interleave with an in-flight single-tile post and leave the **Draft Batch** half-changed.

Everything invariant 6 already says about a single mark still holds here: a **Draft Batch** is not the
**Decision log**, and a select-none is a delete of every row rather than a write of explicit nothings.
"""

from __future__ import annotations

from tests.conftest import Harness
from wallpapi.core import Batch, ResolvedVerdict, SubmissionRefused
from wallpapi.model import Verdict


def test_select_all_marks_every_wallpaper_the_batch_shows(harness: Harness) -> None:
    """Acceptance criterion: select-all applies a chosen **Verdict** across the **Batch**.

    Across what the **Batch** shows, not across the catalogue: the marks have to be readable back as the
    **Draft Batch** the page renders its controls from.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)

    refusal = harness.core.set_all_draft_verdicts(batch.id, Verdict.LIKE)

    assert refusal is None
    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {wallpaper.id: Verdict.LIKE for wallpaper in batch.wallpapers}


def test_select_all_replaces_the_marks_already_set_rather_than_adding_to_them(harness: Harness) -> None:
    """One **Verdict** per **Wallpaper** per submission, as a bulk write as much as a single one.

    A **Favourite** already set must come out as whatever select-all chose, not survive alongside it.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_draft_verdict(batch.id, batch.wallpapers[0].id, Verdict.FAVOURITE)

    harness.core.set_all_draft_verdicts(batch.id, Verdict.BAN)

    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {wallpaper.id: Verdict.BAN for wallpaper in batch.wallpapers}


def test_select_none_deletes_every_mark(harness: Harness) -> None:
    """Acceptance criterion: select-none clears the **Batch**.

    A delete of every row rather than a write of eight explicit nothings — absence already means
    **Ignore**, and a stored "none" would be a second way to say it.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)

    refusal = harness.core.set_all_draft_verdicts(batch.id, None)

    assert refusal is None
    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {}


def test_select_all_then_one_changed_tile_submits_exactly_what_the_screen_showed(
    harness: Harness,
) -> None:
    """The whole point of the pair of operations: bulk first, then correct the one you disagree with.

    What goes into the **Decision log** is what the screen was showing at submit — seven **Likes** and the
    one **Ban** that replaced its **Like** — and not the bulk **Verdict** applied to everything.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, Verdict.LIKE)
    changed = batch.wallpapers[3].id
    harness.core.set_draft_verdict(batch.id, changed, Verdict.BAN)

    harness.core.submit_batch(batch.id)

    recorded = {entry.wallpaper_id: entry.entry for entry in harness.core.list_history(batch_id=batch.id)}
    assert recorded == {
        wallpaper.id: (Verdict.BAN if wallpaper.id == changed else Verdict.LIKE)
        for wallpaper in batch.wallpapers
    }


def test_select_none_then_submit_records_an_ignore_for_every_tile(harness: Harness) -> None:
    """Clearing the **Batch** is not the same as recording nothing.

    Submitting after a select-none still appends an **Ignore** per **Wallpaper**, because absence in the
    **Draft Batch** is exactly what an **Ignore** is derived from.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    harness.core.set_all_draft_verdicts(batch.id, None)

    harness.core.submit_batch(batch.id)

    recorded = harness.core.list_history(batch_id=batch.id)
    assert len(recorded) == len(batch.wallpapers)
    assert all(entry.entry is Verdict.IGNORE for entry in recorded)


def test_bulk_marking_an_unknown_batch_is_refused(harness: Harness) -> None:
    """The same refusal a single mark gives, so the UI keeps one error branch rather than two."""
    refusal = harness.core.set_all_draft_verdicts("no-such-batch", Verdict.FAVOURITE)

    assert isinstance(refusal, SubmissionRefused)
    assert refusal.reason is SubmissionRefused.Reason.UNKNOWN_BATCH
    assert harness.core.list_history() == []


def test_bulk_marking_an_already_submitted_batch_is_refused(harness: Harness) -> None:
    """Invariant 7 reached from the bulk route. Two tabs is a real case: one submits, the other is still
    showing tiles, and "Favourite all" in that stale tab must be told why nothing happened."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.submit_batch(batch.id)
    after_submit = harness.core.list_history()

    refusal = harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)

    assert isinstance(refusal, SubmissionRefused)
    assert refusal.reason is SubmissionRefused.Reason.ALREADY_SUBMITTED
    assert harness.core.list_history() == after_submit


def test_bulk_marking_one_batch_leaves_an_earlier_batchs_record_alone(harness: Harness) -> None:
    """The bulk write is scoped to its own **Batch**.

    Only one unsubmitted **Batch** exists at a time (ADR 0002), so two live **Draft Batches** are not
    reachable through the seam — what is reachable is an earlier **Batch** that has already been recorded.
    A bulk rewrite that forgot its `WHERE batch_id = ?` would still have to leave that record untouched,
    and the later **Batch**'s own marks are its own.
    """
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    harness.core.set_draft_verdict(first.id, first.wallpapers[0].id, Verdict.FAVOURITE)
    harness.core.submit_batch(first.id)
    recorded_before = harness.core.list_history(batch_id=first.id)

    second = harness.core.get_next_batch()
    assert isinstance(second, Batch)
    harness.core.set_all_draft_verdicts(second.id, Verdict.BAN)

    assert harness.core.list_history(batch_id=first.id) == recorded_before
    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.id == second.id
    assert reloaded.drafts == {wallpaper.id: Verdict.BAN for wallpaper in second.wallpapers}


def test_a_batch_bulk_marked_but_never_submitted_records_nothing(harness: Harness) -> None:
    """Invariant 6 again, now that one click can mark the whole **Batch**.

    Marking is not deciding. A bulk **Ban** across eight tiles that is never submitted must leave every
    one of them resolving to nothing at all, or a **Wallpaper** could be written off by a misclick.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)

    harness.core.set_all_draft_verdicts(batch.id, Verdict.BAN)

    assert harness.core.list_history() == []
    assert harness.core.resolve_verdicts([w.id for w in batch.wallpapers]) == {
        w.id: ResolvedVerdict(verdict=None, value=0) for w in batch.wallpapers
    }
