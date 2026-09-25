"""Marking tiles before submitting: the **Draft Batch**.

Invariant 6 — a **Draft Batch** is not the **Decision log**. Nothing here may append an entry; only
submitting does that.
"""

from __future__ import annotations

from tests.conftest import Harness
from wallpapi.core import Batch, ResolvedVerdict, SubmissionRefused
from wallpapi.model import Verdict


def test_marking_a_tile_shows_on_the_live_batch_and_appends_nothing(harness: Harness) -> None:
    """Acceptance criterion: a **Draft Batch** appends nothing to the **Decision log**.

    A page load after a partial draft shows the marks already set, so the mark has to be readable back
    through the seam — that is what the **Batch** page renders its controls from.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    marked = batch.wallpapers[0].id

    harness.core.set_draft_verdict(batch.id, marked, Verdict.FAVOURITE)

    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {marked: Verdict.FAVOURITE}
    assert harness.core.list_history() == []


def test_marking_sets_rather_than_toggles(harness: Harness) -> None:
    """Invariant 6. The same click arriving twice must not flip the mark back off.

    htmx posts are retried and duplicated in the wild, and a toggle would make a replayed click mean the
    opposite of what the user did. The operation takes the **Verdict** it wants to end up with, so sending
    it twice is the same as sending it once.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    marked = batch.wallpapers[0].id

    harness.core.set_draft_verdict(batch.id, marked, Verdict.LIKE)
    harness.core.set_draft_verdict(batch.id, marked, Verdict.LIKE)

    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {marked: Verdict.LIKE}


def test_one_wallpaper_holds_only_one_draft_verdict(harness: Harness) -> None:
    """Acceptance criterion: one **Verdict** per **Wallpaper** per submission.

    Marking a second control replaces the first rather than adding to it — the `(batch, wallpaper)` key
    gives that for free, and this is the test that would notice if the key ever loosened.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    marked = batch.wallpapers[0].id

    harness.core.set_draft_verdict(batch.id, marked, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch.id, marked, Verdict.BAN)

    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {marked: Verdict.BAN}


def test_clearing_a_mark_removes_it(harness: Harness) -> None:
    """Acceptance criterion: clearing deletes the row, because absence already means **Ignore**.

    Storing a "none" **Verdict** would be a second way to say what absence says, and **Verdict resolution**
    would then have two shapes of nothing to handle.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    marked = batch.wallpapers[0].id
    harness.core.set_draft_verdict(batch.id, marked, Verdict.FAVOURITE)

    harness.core.set_draft_verdict(batch.id, marked, None)

    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {}


def test_drafting_against_an_unknown_batch_is_refused(harness: Harness) -> None:
    """Acceptance criterion: refused with a reason, and nothing changes.

    The same refusal type submitting uses, so the UI keeps one error branch rather than growing a second.
    """
    refusal = harness.core.set_draft_verdict("no-such-batch", "wp0000", Verdict.FAVOURITE)

    assert isinstance(refusal, SubmissionRefused)
    assert refusal.reason is SubmissionRefused.Reason.UNKNOWN_BATCH
    assert harness.core.list_history() == []


def test_drafting_against_an_already_submitted_batch_is_refused(harness: Harness) -> None:
    """Invariant 7, now reachable from a second route. Two tabs is a real case: one submits, the other is
    still showing tiles, and a click in that stale tab must be told why nothing happened."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    marked = batch.wallpapers[0].id
    harness.core.submit_batch(batch.id)
    after_submit = harness.core.list_history()

    refusal = harness.core.set_draft_verdict(batch.id, marked, Verdict.FAVOURITE)

    assert isinstance(refusal, SubmissionRefused)
    assert refusal.reason is SubmissionRefused.Reason.ALREADY_SUBMITTED
    assert harness.core.list_history() == after_submit


def test_a_batch_drafted_against_but_never_submitted_records_nothing(harness: Harness) -> None:
    """The #2 criterion, retested now that a **Batch** can be drafted against as well as abandoned.

    Marking a tile is not a decision — only submitting is. **Scores** derive from the **Decision log** and
    nothing else, so a **Wallpaper** must never be written off for having merely been clicked at.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    marks = (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN)
    # strict=False on purpose: three marks against eight tiles, and the other five stay unmarked.
    for wallpaper, verdict in zip(batch.wallpapers, marks, strict=False):
        harness.core.set_draft_verdict(batch.id, wallpaper.id, verdict)

    assert harness.core.list_history() == []
    assert harness.core.resolve_verdicts([w.id for w in batch.wallpapers]) == {
        w.id: ResolvedVerdict(verdict=None, value=0) for w in batch.wallpapers
    }
