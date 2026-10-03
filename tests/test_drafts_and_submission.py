"""Marking tiles in the **Draft Batch**, and submitting it into the **Decision log**.

A **Draft Batch** is not the **Decision log** (invariant 6): marks set rather than toggle, and only
submitting appends. A **Batch** can be submitted once (invariant 7).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import FIXED_NOW, Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, CoreService, ResolvedVerdict, SubmissionRefused
from wallpapi.model import Verdict


def live(harness: Harness) -> Batch:
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    return batch


# -- the Draft Batch ---------------------------------------------------------------------------------


def test_a_mark_is_set_rather_than_toggled_and_reads_back_on_the_live_batch(harness: Harness) -> None:
    """A replayed htmx post must not flip the mark back off; a second control replaces the first, since
    the draft is keyed `(batch, wallpaper)`; and `None` deletes the row, because absence already means
    **Ignore**."""
    batch = live(harness)
    marked = batch.wallpapers[0].id

    harness.core.set_draft_verdict(batch.id, marked, Verdict.FAVOURITE)
    assert live(harness).drafts == {marked: Verdict.FAVOURITE}

    harness.core.set_draft_verdict(batch.id, marked, Verdict.LIKE)
    harness.core.set_draft_verdict(batch.id, marked, Verdict.LIKE)
    assert live(harness).drafts == {marked: Verdict.LIKE}

    harness.core.set_draft_verdict(batch.id, marked, Verdict.BAN)
    assert live(harness).drafts == {marked: Verdict.BAN}

    harness.core.set_draft_verdict(batch.id, marked, None)
    assert live(harness).drafts == {}


def test_select_all_replaces_every_mark_and_select_none_deletes_them(harness: Harness) -> None:
    """One post and one transaction for the whole **Batch**, not one per tile."""
    batch = live(harness)
    harness.core.set_draft_verdict(batch.id, batch.wallpapers[0].id, Verdict.FAVOURITE)

    assert harness.core.set_all_draft_verdicts(batch.id, Verdict.BAN) is None
    assert live(harness).drafts == {w.id: Verdict.BAN for w in batch.wallpapers}

    assert harness.core.set_all_draft_verdicts(batch.id, None) is None
    assert live(harness).drafts == {}


def test_select_all_then_one_changed_tile_submits_exactly_what_the_screen_showed(harness: Harness) -> None:
    batch = live(harness)
    harness.core.set_all_draft_verdicts(batch.id, Verdict.LIKE)
    changed = batch.wallpapers[3].id
    harness.core.set_draft_verdict(batch.id, changed, Verdict.BAN)

    harness.core.submit_batch(batch.id)

    recorded = {entry.wallpaper_id: entry.entry for entry in harness.core.list_history(batch_id=batch.id)}
    assert recorded == {w.id: (Verdict.BAN if w.id == changed else Verdict.LIKE) for w in batch.wallpapers}


def test_bulk_marking_one_batch_leaves_an_earlier_batchs_record_alone(harness: Harness) -> None:
    """Only one unsubmitted **Batch** exists at a time, so the reachable neighbour of a bulk write that
    forgot its `WHERE batch_id = ?` is an earlier, recorded **Batch**."""
    first = live(harness)
    harness.core.set_draft_verdict(first.id, first.wallpapers[0].id, Verdict.FAVOURITE)
    harness.core.submit_batch(first.id)
    recorded_before = harness.core.list_history(batch_id=first.id)

    second = live(harness)
    harness.core.set_all_draft_verdicts(second.id, Verdict.BAN)

    assert harness.core.list_history(batch_id=first.id) == recorded_before
    reloaded = live(harness)
    assert reloaded.id == second.id
    assert reloaded.drafts == {w.id: Verdict.BAN for w in second.wallpapers}


def _mark_three(harness: Harness, batch: Batch) -> None:
    marks = (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN)
    for wallpaper, verdict in zip(batch.wallpapers, marks, strict=False):
        harness.core.set_draft_verdict(batch.id, wallpaper.id, verdict)


def _ban_all(harness: Harness, batch: Batch) -> None:
    harness.core.set_all_draft_verdicts(batch.id, Verdict.BAN)


def _show_twice(harness: Harness, batch: Batch) -> None:
    del batch
    harness.core.get_next_batch()


@pytest.mark.parametrize(
    "act", [_show_twice, _mark_three, _ban_all], ids=["shown and reloaded", "tiles marked", "all banned"]
)
def test_a_batch_that_is_never_submitted_records_nothing(
    harness: Harness, act: Callable[[Harness, Batch], None]
) -> None:
    """Being shown or clicked at is not a **Verdict**. **Scores** derive from the **Decision log** alone,
    so a **Wallpaper** must never be written off by a misclick."""
    batch = live(harness)

    act(harness, batch)

    assert harness.core.list_history() == []
    assert harness.core.resolve_verdicts([w.id for w in batch.wallpapers]) == {
        w.id: ResolvedVerdict(verdict=None, value=0) for w in batch.wallpapers
    }


# -- refusals ----------------------------------------------------------------------------------------

OPERATIONS: dict[str, Callable[[CoreService, str], object]] = {
    "mark": lambda core, batch_id: core.set_draft_verdict(batch_id, "wp0000", Verdict.FAVOURITE),
    "mark all": lambda core, batch_id: core.set_all_draft_verdicts(batch_id, Verdict.FAVOURITE),
    "submit": lambda core, batch_id: core.submit_batch(batch_id),
}


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("already_submitted", [False, True], ids=["unknown batch", "already submitted"])
def test_an_unknown_or_already_submitted_batch_is_refused_and_records_nothing(
    harness: Harness, operation: str, already_submitted: bool
) -> None:
    """Two tabs is a real case: one submits, and a click in the stale one must be told why nothing
    happened. One refusal type for all three, so the UI keeps one error branch. Three operations because
    each checks for itself."""
    batch_id = "no-such-batch"
    if already_submitted:
        batch_id = live(harness).id
        harness.core.submit_batch(batch_id)
    before = harness.core.list_history()

    refusal = OPERATIONS[operation](harness.core, batch_id)

    assert isinstance(refusal, SubmissionRefused)
    expected = (
        SubmissionRefused.Reason.ALREADY_SUBMITTED
        if already_submitted
        else SubmissionRefused.Reason.UNKNOWN_BATCH
    )
    assert refusal.reason is expected
    assert harness.core.list_history() == before


# -- submitting --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cleared_first", [False, True], ids=["untouched", "select-all then select-none"])
def test_an_empty_submission_records_an_ignore_for_every_wallpaper_shown(
    harness: Harness, cleared_first: bool
) -> None:
    """Clearing the **Batch** is not recording nothing: absence in the draft is what an **Ignore** is
    derived from. **Ignores** never download anything."""
    batch = live(harness)
    if cleared_first:
        harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
        harness.core.set_all_draft_verdicts(batch.id, None)

    harness.core.submit_batch(batch.id)

    history = harness.core.list_history()
    assert {e.wallpaper_id for e in history} == {w.id for w in batch.wallpapers}
    assert [e.entry for e in history] == [Verdict.IGNORE] * 8
    assert all(e.batch_id == batch.id for e in history)
    assert harness.library.written == []


def test_submitting_records_the_drafted_verdicts_plus_an_ignore_for_everything_unmarked(
    harness: Harness,
) -> None:
    batch = live(harness)
    favourite, like, ban, *unmarked = [w.id for w in batch.wallpapers]
    harness.core.set_draft_verdict(batch.id, favourite, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch.id, like, Verdict.LIKE)
    harness.core.set_draft_verdict(batch.id, ban, Verdict.BAN)

    harness.core.submit_batch(batch.id)

    recorded = {e.wallpaper_id: e.entry for e in harness.core.list_history(batch_id=batch.id)}
    assert recorded == {
        favourite: Verdict.FAVOURITE,
        like: Verdict.LIKE,
        ban: Verdict.BAN,
        **{wallpaper_id: Verdict.IGNORE for wallpaper_id in unmarked},
    }


def test_the_whole_submission_shares_one_timestamp_and_an_ascending_sequence(harness: Harness) -> None:
    """One transaction, since an append-only log cannot retract a half-recorded **Batch**. The shared
    timestamp is what makes the sequence load-bearing (invariant 4)."""
    batch = live(harness)
    harness.core.set_draft_verdict(batch.id, batch.wallpapers[0].id, Verdict.FAVOURITE)

    harness.core.submit_batch(batch.id)

    history = harness.core.list_history(batch_id=batch.id)
    assert len(history) == 8
    assert {e.recorded_at for e in history} == {FIXED_NOW}
    assert [e.seq for e in history] == sorted({e.seq for e in history})


def test_a_draft_against_a_wallpaper_this_batch_does_not_show_records_nothing(db_path: Path) -> None:
    """No control can post this, but a hand-made post can put a stray draft row there. Entries derive from
    what the **Batch** showed, not from what the drafts name."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    harness.core.update_settings(batch_size=2)
    first = live(harness)
    second = harness.core.submit_batch(first.id)
    assert isinstance(second, Batch)
    shown = {w.id for w in second.wallpapers}
    stray = next(w.id for w in first.wallpapers if w.id not in shown)
    harness.core.set_draft_verdict(second.id, stray, Verdict.FAVOURITE)

    harness.core.submit_batch(second.id)

    assert {e.wallpaper_id for e in harness.core.list_history(batch_id=second.id)} == shown
    assert harness.core.resolve_verdicts([stray])[stray].verdict is Verdict.IGNORE


def test_submitting_returns_a_fresh_batch(harness: Harness) -> None:
    first = live(harness)

    second = harness.core.submit_batch(first.id)

    assert isinstance(second, Batch)
    assert second.id != first.id
    assert len(second.wallpapers) == 8


def test_a_second_ignore_is_appended_not_folded_into_the_first(db_path: Path) -> None:
    """Append-only. **History** is the only way to decide a submitted **Wallpaper** again."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = live(harness)
    harness.core.submit_batch(first.id)

    for wallpaper in first.wallpapers:
        assert harness.core.edit_verdict(wallpaper.id, Verdict.IGNORE) is None

    history = harness.core.list_history()
    assert len(history) == 16
    for wallpaper in first.wallpapers:
        assert [e.entry for e in history if e.wallpaper_id == wallpaper.id] == [
            Verdict.IGNORE,
            Verdict.IGNORE,
        ]


def test_the_decision_log_survives_a_restart(db_path: Path) -> None:
    """A second Core service over the same file, which also proves the migrations are idempotent."""
    first_run = make_harness(db_path)
    first_run.core.submit_batch(live(first_run).id)
    recorded = first_run.core.list_history()

    restored = make_harness(db_path).core.list_history()

    assert len(restored) == 8
    assert [(e.wallpaper_id, e.entry, e.recorded_at) for e in restored] == [
        (e.wallpaper_id, e.entry, e.recorded_at) for e in recorded
    ]
