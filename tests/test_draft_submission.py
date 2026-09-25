"""Submitting a drafted **Batch**: what the **Draft Batch** becomes in the **Decision log**."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FIXED_NOW, Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch
from wallpapi.model import Verdict


def test_submitting_records_the_drafted_verdicts_plus_an_ignore_for_everything_unmarked(
    harness: Harness,
) -> None:
    """The acceptance criterion at the heart of the ticket.

    Three tiles are marked and five are left alone. The three go in as they were drafted, and the five
    become **Ignores** — the implicit **Verdict** every unpicked **Wallpaper** in a submitted **Batch**
    gets. One entry per **Wallpaper** shown, and no entry for anything that was not.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    favourite, like, ban, *unmarked = [w.id for w in batch.wallpapers]
    harness.core.set_draft_verdict(batch.id, favourite, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch.id, like, Verdict.LIKE)
    harness.core.set_draft_verdict(batch.id, ban, Verdict.BAN)

    harness.core.submit_batch(batch.id)

    recorded = {e.wallpaper_id: e.verdict for e in harness.core.list_history(batch_id=batch.id)}
    assert recorded == {
        favourite: Verdict.FAVOURITE,
        like: Verdict.LIKE,
        ban: Verdict.BAN,
        **{wallpaper_id: Verdict.IGNORE for wallpaper_id in unmarked},
    }


def test_the_whole_submission_shares_one_timestamp_and_an_ascending_sequence(harness: Harness) -> None:
    """One transaction, so the **Explicit Verdicts** and the derived **Ignores** land together.

    A half-recorded **Batch** cannot be retracted — the **Decision log** is append-only — so the two halves
    must not be two writes. The shared timestamp is what makes the sequence load-bearing (invariant 4).
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_draft_verdict(batch.id, batch.wallpapers[0].id, Verdict.FAVOURITE)

    harness.core.submit_batch(batch.id)

    history = harness.core.list_history(batch_id=batch.id)
    assert len(history) == 8
    assert {e.recorded_at for e in history} == {FIXED_NOW}
    assert [e.seq for e in history] == sorted({e.seq for e in history})


def test_a_draft_against_a_wallpaper_this_batch_does_not_show_records_nothing(db_path: Path) -> None:
    """A submission records one entry per **Wallpaper** shown, and nothing for anything that was not.

    No control can post this — the marks come from tiles that are on the page — but the **Draft Batch** is
    keyed only by **Batch** and **Wallpaper**, so a hand-made post naming a **Wallpaper** from an earlier
    **Batch** puts a stray row there. Submitting must derive its entries from what the **Batch** actually
    showed rather than from what the drafts happen to name, or a **Wallpaper** could collect a **Verdict**
    in a **Batch** it never appeared in.

    A **Wallpaper** this database has never stored cannot be drafted against at all — `draft_batch`
    references `wallpapers`, and foreign keys are on — so the reachable stray is a stored one.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    harness.core.set_setting("batch_size", "2")
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    second = harness.core.submit_batch(first.id)
    assert isinstance(second, Batch)
    shown = {w.id for w in second.wallpapers}
    stray = next(w.id for w in first.wallpapers if w.id not in shown)
    harness.core.set_draft_verdict(second.id, stray, Verdict.FAVOURITE)

    harness.core.submit_batch(second.id)

    assert {e.wallpaper_id for e in harness.core.list_history(batch_id=second.id)} == shown
    assert harness.core.resolve_verdicts([stray])[stray].verdict is Verdict.IGNORE
