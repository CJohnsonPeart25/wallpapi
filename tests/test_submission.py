"""Submitting a Batch and what lands in the Decision log. Issue #2 acceptance criteria 2, 3 and 7."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FIXED_NOW, Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, SubmissionRefused
from wallpapi.model import Verdict


def test_empty_submission_records_an_ignore_for_every_wallpaper_in_the_batch(harness: Harness) -> None:
    """The acceptance criterion issue #2 names explicitly.

    Nothing is picked, so every Wallpaper in the Batch is recorded as an Ignore — and only those. The
    Library writer must not be touched: Ignores never download anything.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = {w.id for w in batch.wallpapers}

    harness.core.submit_batch(batch.id)

    history = harness.core.list_history()
    assert {e.wallpaper_id for e in history} == shown
    assert [e.verdict for e in history] == [Verdict.IGNORE] * 8
    assert all(e.batch_id == batch.id for e in history)
    assert harness.library.written == []


def test_a_batch_that_is_never_submitted_records_nothing(harness: Harness) -> None:
    """Being shown is not a **Verdict**.

    A **Batch** sitting on screen, refreshed past or abandoned, must leave the **Decision log** untouched.
    **Scores** derive from the **Decision log** and nothing else, so a **Wallpaper** must never be written
    off merely for having appeared. Only submitting appends.
    """
    first = harness.core.get_next_batch()
    harness.core.get_next_batch()

    assert isinstance(first, Batch)
    assert harness.core.list_history() == []


def test_submitting_returns_a_fresh_batch(harness: Harness) -> None:
    """Acceptance criterion: submitting returns a new Batch, so the user keeps going without a reload."""
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)

    second = harness.core.submit_batch(first.id)

    assert isinstance(second, Batch)
    assert second.id != first.id
    assert len(second.wallpapers) == 8


def test_ignores_stack_across_two_batches_containing_the_same_wallpaper(db_path: Path) -> None:
    """The Decision log is append-only: a second Ignore is appended, not folded into the first.

    The catalogue holds exactly 8, so both Batches contain all 8 and every Wallpaper is Ignored twice.
    Verdict resolution itself is #3; all that is asserted here is that both entries survive.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    harness.core.submit_batch(first.id)
    second = harness.core.get_next_batch()
    assert isinstance(second, Batch)

    harness.core.submit_batch(second.id)

    history = harness.core.list_history()
    assert len(history) == 16
    for wallpaper_id in (w.id for w in first.wallpapers):
        assert [e.verdict for e in history if e.wallpaper_id == wallpaper_id] == [
            Verdict.IGNORE,
            Verdict.IGNORE,
        ]


def test_decision_log_survives_a_restart(db_path: Path) -> None:
    """Acceptance criterion: the Decision log persists across restarts.

    A second Core service is built over the same database file, which also proves migrations are
    idempotent — they must not assume an empty database on the second run.
    """
    first_run = make_harness(db_path)
    batch = first_run.core.get_next_batch()
    assert isinstance(batch, Batch)
    first_run.core.submit_batch(batch.id)
    recorded = first_run.core.list_history()

    second_run = make_harness(db_path)
    restored = second_run.core.list_history()

    assert len(restored) == 8
    assert [(e.wallpaper_id, e.verdict) for e in restored] == [(e.wallpaper_id, e.verdict) for e in recorded]
    assert [e.recorded_at for e in restored] == [e.recorded_at for e in recorded]


def test_resubmitting_a_batch_is_refused_and_appends_nothing(harness: Harness) -> None:
    """Two browser tabs can submit the same Batch twice. The second must be told why nothing happened.

    A silent no-op would be idempotent but invisible, which is the worst of both: the second tab sits
    there having apparently done something. The Decision log must be unchanged.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.submit_batch(batch.id)
    after_first = harness.core.list_history()

    refusal = harness.core.submit_batch(batch.id)

    assert isinstance(refusal, SubmissionRefused)
    assert refusal.reason is SubmissionRefused.Reason.ALREADY_SUBMITTED
    assert harness.core.list_history() == after_first


def test_submitting_an_unknown_batch_id_is_refused(harness: Harness) -> None:
    """Same refusal shape as a resubmission, so the UI has one error branch rather than two."""
    refusal = harness.core.submit_batch("no-such-batch")

    assert isinstance(refusal, SubmissionRefused)
    assert refusal.reason is SubmissionRefused.Reason.UNKNOWN_BATCH
    assert harness.core.list_history() == []


def test_entries_from_one_submission_have_a_stable_distinct_order(harness: Harness) -> None:
    """Verdict resolution orders by sequence, not timestamp — and #3 depends on this holding.

    The clock is frozen, so all 8 entries share a timestamp to the microsecond. If ordering rested on the
    timestamp, "the latest Explicit Verdict wins" would be undefined the moment two Verdicts land in one
    submit transaction. The sequence must be distinct and ascending regardless.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)

    harness.core.submit_batch(batch.id)

    history = harness.core.list_history()
    sequences = [e.seq for e in history]
    assert len(set(sequences)) == 8
    assert sequences == sorted(sequences)
    assert {e.recorded_at for e in history} == {FIXED_NOW}
