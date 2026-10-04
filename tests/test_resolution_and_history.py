"""**Verdict resolution**, **History** edits and the **History** listing.

The latest entry decides (ADR 0015), ordered by sequence rather than timestamp (invariant 4). Entries are made
the way the app makes them, by submitting and by `edit_verdict`; the legacy **Clearance** is the one written
behind the seam, because nothing can make one any more.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.conftest import Harness, judge, live, make_harness, serving, submit_with, write_legacy_clearance
from tests.fakes import catalogue_of, wallpaper
from wallpapi import decisions, workflows
from wallpapi.batches import Batch, BatchUnavailable
from wallpapi.decisions import HISTORY_PAGE_SIZE, HistoryEntry, HistoryRefused, ResolvedVerdict
from wallpapi.model import Verdict

SUBJECT = "wp0000"


def test_a_wallpaper_with_no_entries_resolves_to_nothing_worth_zero(harness: Harness) -> None:
    """Zero rather than absent, because **Scores** sum these across the whole **Pool**."""
    assert decisions.resolve(harness.connect(), ["never-seen"])["never-seen"] == ResolvedVerdict(
        verdict=None, value=0
    )


def test_each_explicit_verdict_resolves_to_its_own_value(db_path: Path) -> None:
    """The spec's numbers as literals, so this disagrees with the implementation if it drifts."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    submit_with(harness, {"wp0000": Verdict.FAVOURITE, "wp0001": Verdict.LIKE, "wp0002": Verdict.BAN})

    resolved = decisions.resolve(harness.connect(), ["wp0000", "wp0001", "wp0002"])

    assert resolved["wp0000"] == ResolvedVerdict(verdict=Verdict.FAVOURITE, value=100)
    assert resolved["wp0001"] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)
    assert resolved["wp0002"] == ResolvedVerdict(verdict=Verdict.BAN, value=-100)


@pytest.mark.parametrize(
    ("submitted", "edits", "resolved"),
    [
        # Passing something over twice is not twice the dislike, and "ignored" is not "never seen".
        pytest.param(None, [Verdict.IGNORE], ResolvedVerdict(Verdict.IGNORE, -10), id="ignores do not stack"),
        pytest.param(
            None,
            [Verdict.IGNORE, Verdict.LIKE],
            ResolvedVerdict(Verdict.LIKE, 50),
            id="a verdict overturns ignores",
        ),
        pytest.param(
            Verdict.LIKE,
            [Verdict.FAVOURITE],
            ResolvedVerdict(Verdict.FAVOURITE, 100),
            id="replaced, not added to",
        ),
        pytest.param(
            Verdict.LIKE,
            [Verdict.IGNORE],
            ResolvedVerdict(Verdict.IGNORE, -10),
            id="an ignore withdraws a verdict",
        ),
    ],
)
def test_the_latest_entry_decides(
    db_path: Path, submitted: Verdict | None, edits: list[Verdict], resolved: ResolvedVerdict
) -> None:
    """Submitted in a **Batch**, then edited from **History**: what came before the latest counts for
    nothing. An edit carries no **Batch**, which is how the log tells it apart."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    submit_with(harness, {SUBJECT: submitted})

    for verdict in edits:
        assert isinstance(workflows.edit_verdict(harness.modules, SUBJECT, verdict), HistoryEntry)

    assert decisions.resolve(harness.connect(), [SUBJECT])[SUBJECT] == resolved
    assert decisions.entries(harness.connect(), wallpaper_id=SUBJECT)[-1].batch_id is None


def test_two_history_edits_sharing_a_timestamp_resolve_by_sequence(db_path: Path) -> None:
    """Two clicks share a whole-second `recorded_at`, so ordering by timestamp would be a coin toss."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    submit_with(harness, {SUBJECT: Verdict.FAVOURITE})

    workflows.edit_verdict(harness.modules, SUBJECT, Verdict.IGNORE)
    workflows.edit_verdict(harness.modules, SUBJECT, Verdict.LIKE)

    entries = decisions.entries(harness.connect(), wallpaper_id=SUBJECT)
    assert [e.entry for e in entries] == [Verdict.FAVOURITE, Verdict.IGNORE, Verdict.LIKE]
    assert len({e.recorded_at for e in entries}) == 1, "the clock must not have moved"
    assert decisions.resolve(harness.connect(), [SUBJECT])[SUBJECT].verdict is Verdict.LIKE


def test_a_legacy_clearance_resolves_to_nothing_when_it_is_the_latest_entry(db_path: Path) -> None:
    """A database from before ADR 0015 may hold one, and the log is append-only. Anything after it decides
    as usual."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    cleared, judged_again = "wp0000", "wp0001"
    submit_with(harness, {cleared: Verdict.FAVOURITE, judged_again: Verdict.BAN})
    write_legacy_clearance(db_path, cleared, judged_again)
    assert isinstance(workflows.edit_verdict(harness.modules, judged_again, Verdict.LIKE), HistoryEntry)

    resolved = decisions.resolve(harness.connect(), [cleared, judged_again])

    assert resolved[cleared] == ResolvedVerdict(verdict=None, value=0)
    assert resolved[judged_again] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)


# -- History edits -----------------------------------------------------------------------------------


def test_an_edit_appends_and_never_rewrites(db_path: Path) -> None:
    """The earlier entry stays, with its **Batch**; the edit is not counted among what the **Batch**
    recorded."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    submitted = submit_with(harness, {SUBJECT: Verdict.LIKE})

    assert isinstance(workflows.edit_verdict(harness.modules, SUBJECT, Verdict.BAN), HistoryEntry)

    entries = decisions.entries(harness.connect(), wallpaper_id=SUBJECT)
    assert [(e.entry, e.batch_id) for e in entries] == [(Verdict.LIKE, submitted.id), (Verdict.BAN, None)]
    from_the_batch = decisions.entries(harness.connect(), batch_id=submitted.id, wallpaper_id=SUBJECT)
    assert [e.entry for e in from_the_batch] == [Verdict.LIKE], "the edit is not part of the Batch"


def test_editing_an_unknown_wallpaper_is_refused_and_appends_nothing(harness: Harness) -> None:
    """A refusal in the house style rather than the foreign key's `IntegrityError`."""
    before = decisions.entries(harness.connect())

    refused = workflows.edit_verdict(harness.modules, "never-seen", Verdict.IGNORE)

    assert refused == HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
    assert decisions.entries(harness.connect()) == before
    assert harness.library.written == []


def test_a_ban_from_history_excludes_a_wallpaper_and_an_ignore_makes_it_eligible_again(db_path: Path) -> None:
    """Un-**Banning** falls out of building **Batches** by *resolved* **Verdict**. Banned before any
    **Batch** showed it, since a shown **Wallpaper** has left the **Pool** for good (ADR 0016)."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    workflows.save_settings(harness.modules, batch_size=1)
    assert isinstance(workflows.edit_verdict(harness.modules, SUBJECT, Verdict.BAN), HistoryEntry)
    assert isinstance(harness.modules.batches.next(harness.connect()), BatchUnavailable), (
        "the Ban must exclude it"
    )

    assert isinstance(workflows.edit_verdict(harness.modules, SUBJECT, Verdict.IGNORE), HistoryEntry)

    reoffered = live(harness)
    assert [w.id for w in reoffered.wallpapers] == [SUBJECT]
    assert reoffered.drafts == {}, "an Ignore is not pre-filled: the tile comes up unmarked"


def test_a_history_edit_does_not_reach_the_batch_already_open(db_path: Path) -> None:
    """Deliberately not handled (ADR 0015): the open tile is unmarked, so submitting it untouched records
    an **Ignore**, which is the latest entry and overturns the edit. The tile shows what it will record."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    workflows.save_settings(harness.modules, batch_size=1)
    open_batch = live(harness)

    assert isinstance(workflows.edit_verdict(harness.modules, SUBJECT, Verdict.LIKE), HistoryEntry)
    submitted = submit_with(harness, {})

    assert submitted == open_batch, "the edit neither rerolled nor re-marked the open Batch"
    assert decisions.resolve(harness.connect(), [SUBJECT])[SUBJECT].verdict is Verdict.IGNORE


# -- a reshown Wallpaper comes up marked (dormant until re-evaluation, ADR 0015) ------------------------


def decided_before_it_is_drawn(harness: Harness, verdicts: Mapping[str, Verdict]) -> Batch:
    """Decide **Pool** members from **History**, then mint: the one way the seam still reaches a **Batch**
    drawing a decided **Wallpaper**, since an edit takes nothing out of the **Pool**."""
    for wallpaper_id, verdict in verdicts.items():
        assert isinstance(workflows.edit_verdict(harness.modules, wallpaper_id, verdict), HistoryEntry)
    batch = live(harness)
    return batch


def test_a_reshown_wallpaper_comes_up_marked_with_its_latest_verdict(db_path: Path) -> None:
    """Minted with each **Explicit Verdict** already in the **Draft Batch**, wherever it was decided. An
    **Ignore** is not pre-filled, and a **Ban** is never drawn."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    liked, favourite, banned, ignored, overturned = "wp0000", "wp0001", "wp0002", "wp0003", "wp0004"
    assert isinstance(workflows.edit_verdict(harness.modules, overturned, Verdict.FAVOURITE), HistoryEntry)

    reshown = decided_before_it_is_drawn(
        harness,
        {
            liked: Verdict.LIKE,
            favourite: Verdict.FAVOURITE,
            banned: Verdict.BAN,
            ignored: Verdict.IGNORE,
            overturned: Verdict.LIKE,
        },
    )

    assert reshown.drafts == {liked: Verdict.LIKE, favourite: Verdict.FAVOURITE, overturned: Verdict.LIKE}
    assert ignored in {w.id for w in reshown.wallpapers}
    assert banned not in {w.id for w in reshown.wallpapers}, "a Ban is never reshown"
    assert reshown == harness.modules.batches.next(harness.connect()), (
        "read back from storage, so a reload keeps it"
    )


def test_leaving_a_reshown_wallpaper_alone_records_its_verdict_again(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    decided_before_it_is_drawn(harness, {SUBJECT: Verdict.LIKE})

    submit_with(harness, {})

    assert [e.entry for e in decisions.entries(harness.connect(), wallpaper_id=SUBJECT)] == [
        Verdict.LIKE,
        Verdict.LIKE,
    ]
    assert decisions.resolve(harness.connect(), [SUBJECT])[SUBJECT] == ResolvedVerdict(
        verdict=Verdict.LIKE, value=50
    )


@pytest.mark.parametrize("select_none", [False, True], ids=["unmarking the tile", "select-none"])
def test_unmarking_a_reshown_wallpaper_overturns_its_verdict(
    db_path: Path, tmp_path: Path, select_none: bool
) -> None:
    """The **Batch** half of ADR 0015: the **Ignore** written at submission is the latest entry, and the
    **Library** follows it. Select-none is a decision about the pre-filled tiles too."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    workflows.save_settings(harness.modules, library_path=tmp_path / "Library")
    batch = decided_before_it_is_drawn(harness, {SUBJECT: Verdict.FAVOURITE})
    written = harness.library.written[0].destination
    assert batch.drafts == {SUBJECT: Verdict.FAVOURITE}

    if select_none:
        assert isinstance(workflows.set_all_drafts(harness.modules, batch.id, None), Batch)
        workflows.submit(harness.modules, batch.id)
    else:
        submit_with(harness, {SUBJECT: None})

    assert decisions.resolve(harness.connect(), [SUBJECT])[SUBJECT] == ResolvedVerdict(
        verdict=Verdict.IGNORE, value=-10
    )
    assert harness.library.removed == [written]


# -- the History listing -----------------------------------------------------------------------------


def test_an_untouched_pool_has_no_history(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(4))

    listing = decisions.history(harness.connect())

    assert (listing.entries, listing.total, listing.pages) == ((), 0, 1)


def test_rows_carry_the_wallpaper_its_resolved_verdict_and_its_latest_timestamp(db_path: Path) -> None:
    """The *resolved* **Verdict**, which is the difference between **History** and a print-out of the
    log. Read off the page, which is where the **Wallpaper** is joined on."""
    harness = make_harness(db_path, catalogue=(wallpaper("only"),))
    workflows.save_settings(harness.modules, batch_size=1)
    submit_with(harness, {"only": Verdict.LIKE})

    with serving(harness) as client:
        page = client.get("/history").text

    assert 'data-wallpaper-id="only"' in page
    assert 'href="https://wallhaven.cc/w/only"' in page
    assert 'data-resolved-verdict="like"' in page
    assert f'<time datetime="{harness.clock.now().isoformat()}">' in page


def test_one_row_per_wallpaper_however_many_entries_it_has(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=(wallpaper("only"),))
    judge(harness, only=Verdict.LIKE)
    judge(harness, only=Verdict.IGNORE)
    judge(harness, only=Verdict.BAN)

    listing = decisions.history(harness.connect())

    assert listing.total == 1
    assert listing.entries[0].resolved.verdict is Verdict.BAN
    assert len(decisions.entries(harness.connect(), wallpaper_id="only")) == 3


def test_rows_are_ordered_by_newest_activity(db_path: Path) -> None:
    """By sequence, since every entry here shares a `recorded_at` (invariant 4)."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    judge(harness, wp0000=Verdict.LIKE, wp0001=Verdict.BAN, wp0002=Verdict.FAVOURITE)
    judge(harness, wp0001=Verdict.LIKE)

    listing = decisions.history(harness.connect())

    assert [row.wallpaper_id for row in listing.entries] == ["wp0001", "wp0002", "wp0000"]
    assert len({row.decided_at for row in listing.entries}) == 1, "the clock must not have moved"


def test_the_filter_narrows_to_one_resolved_verdict_ignore_included(db_path: Path) -> None:
    """Filtered by what a **Wallpaper** resolves to now, not what it was ever given: the **Banned** one
    was **Liked** first, and the withdrawn **Favourite** is listed under **Ignore**, where the thousands
    of submitted **Ignores** also are."""
    harness = make_harness(db_path, catalogue=catalogue_of(5))
    workflows.save_settings(harness.modules, batch_size=5)
    submit_with(
        harness, {"wp0000": Verdict.LIKE, "wp0001": Verdict.LIKE, "wp0002": Verdict.FAVOURITE, "wp0003": None}
    )
    judge(harness, wp0001=Verdict.BAN, wp0002=Verdict.IGNORE)

    def listed(verdict: Verdict) -> set[str]:
        return {row.wallpaper_id for row in decisions.history(harness.connect(), verdict=verdict).entries}

    assert listed(Verdict.LIKE) == {"wp0000"}
    assert listed(Verdict.BAN) == {"wp0001"}
    assert listed(Verdict.FAVOURITE) == set()
    assert listed(Verdict.IGNORE) == {"wp0002", "wp0003", "wp0004"}
    assert decisions.history(harness.connect(), verdict=Verdict.LIKE).total == 1


def test_pages_are_a_hundred_rows_newest_first(db_path: Path) -> None:
    """A page number past the end is clamped rather than refused, so a stale link lands somewhere."""
    count = HISTORY_PAGE_SIZE + 1
    harness = make_harness(db_path, catalogue=catalogue_of(count), page_size=count)
    judge(harness, **{f"wp{n:04d}": Verdict.LIKE for n in range(count)})

    first = decisions.history(harness.connect())
    second = decisions.history(harness.connect(), page=2)

    assert first.total == count
    assert first.pages == 2
    assert len(first.entries) == HISTORY_PAGE_SIZE
    assert first.entries[0].wallpaper_id == f"wp{count - 1:04d}"
    assert [row.wallpaper_id for row in second.entries] == ["wp0000"]
    assert decisions.history(harness.connect(), page=99).page == 2
    assert decisions.history(harness.connect(), page=0).page == 1


def test_a_single_row_reads_back_what_an_edit_left(db_path: Path) -> None:
    """What the page swaps in after an edit."""
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    judge(harness, wp0000=Verdict.LIKE)

    row = decisions.history_entry(harness.connect(), "wp0000")

    assert row is not None
    assert row.resolved.verdict is Verdict.LIKE
    assert decisions.history_entry(harness.connect(), "wp0001") is None, "an unjudged Wallpaper has no row"
    assert decisions.history_entry(harness.connect(), "never-seen") is None
