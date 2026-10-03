"""Editing **Mixes** and making new ones. Issue #12.

Everything enters through the Core service (invariant 1). A **Mix** is only observable in two ways —
`list_mixes` says what there is, and a **Batch** drawn under it says what it does — so every test here
asserts on one of those rather than on the row behind them.

The **Zones** a draw needs are arranged by `tests/zoned.py`, the same way `test_mix.py` arranges them: a
**Zone** cannot be set through the seam, so one **Favourite** and one **Ban** spread their values into a
**Pool** the two of them are no longer part of.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tests.conftest import make_harness
from tests.zoned import ZonedPool, zoned_pool
from wallpapi.core import (
    EXPLORE_MIX,
    MAX_MIX_NAME_LENGTH,
    REFINE_MIX,
    Batch,
    SettingsRefused,
)
from wallpapi.model import Mix, Zone

EDITED_EXPLORE = Mix(name="explore", unknown=50, banger=45, dud=5)
"""**Explore** with the **Bangers** turned up. At a **Batch** of 20 every **Slot** is guaranteed — 10, 9
and 1 — so what a **Batch** drawn under it looks like is arithmetic rather than a roll."""

EXACT_BATCH = 20
"""A size that divides every percentage of `EDITED_EXPLORE` exactly, so there is no leftover **Slot** and
the assertion can be an equality instead of a bound."""


def _drawn(pool: ZonedPool, size: int) -> Batch:
    """One **Batch** of `size` off an arranged **Pool**."""
    pool.harness.core.update_settings(batch_size=size)
    batch = pool.harness.core.get_next_batch()
    assert isinstance(batch, Batch), batch
    return batch


def _zone_counts(batch: Batch) -> Counter[Zone]:
    return Counter(batch.zones[w.id] for w in batch.wallpapers)


# -- editing what is there -------------------------------------------------------------------------


def test_editing_explore_changes_what_the_next_batch_is_made_of(db_path: Path) -> None:
    """The acceptance criterion that matters most: an edited **Mix** drives **Allocation**.

    **Explore** is saved at 50/45/5 and the next **Batch** of 20 is 10 **Unknowns**, 9 **Bangers** and 1
    **Dud** — every **Slot** guaranteed at that size, so this is the edit arriving intact rather than a
    roll that happened to land well. The **Pool** holds far more of each than the draw asks for, so no
    part of this is the shortfall rule.
    """
    pool = zoned_pool(db_path, bangers=30, duds=30, unknowns=60)

    saved = pool.harness.core.save_mix("explore", unknown=50, banger=45, dud=5)

    assert saved == EDITED_EXPLORE
    assert pool.harness.core.active_mix() == EDITED_EXPLORE
    counts = _zone_counts(_drawn(pool, EXACT_BATCH))
    assert counts == Counter({Zone.UNKNOWN: 10, Zone.BANGER: 9, Zone.DUD: 1})


def test_editing_a_mix_does_not_change_which_one_is_active(db_path: Path) -> None:
    """Editing **Refine** while **Explore** is in force edits **Refine** and nothing else. Saving a
    **Mix** is not a way of selecting one."""
    harness = make_harness(db_path)

    harness.core.save_mix("refine", unknown=10, banger=90, dud=0)

    assert harness.core.get_settings().active_mix == "explore"
    assert harness.core.active_mix() == EXPLORE_MIX
    assert Mix(name="refine", unknown=10, banger=90, dud=0) in harness.core.list_mixes()


def test_an_edited_mix_survives_a_restart(db_path: Path) -> None:
    """Stored rather than held in memory. A second Core service over the same file finds the edit and the
    custom **Mix**, not the values migration 7 seeded."""
    first = make_harness(db_path)
    first.core.save_mix("explore", unknown=50, banger=45, dud=5)
    first.core.save_mix("all in", unknown=0, banger=100, dud=0)

    restarted = make_harness(db_path)

    assert restarted.core.list_mixes() == (
        Mix(name="all in", unknown=0, banger=100, dud=0),
        EDITED_EXPLORE,
        REFINE_MIX,
    )


# -- making and removing -------------------------------------------------------------------------


def test_a_custom_mix_can_be_made_selected_and_drawn_under(db_path: Path) -> None:
    """The acceptance criterion for a custom **Mix**, end to end.

    Named, saved, selected with `update_settings(active_mix=...)` — which refused every name before this
    ticket — and then the thing the next **Batch** is built from.
    """
    pool = zoned_pool(db_path, bangers=30, duds=30, unknowns=60)

    made = pool.harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)
    pool.harness.core.update_settings(active_mix="duds only")

    assert made == Mix(name="duds only", unknown=0, banger=0, dud=100)
    assert pool.harness.core.active_mix() == made
    assert _zone_counts(_drawn(pool, EXACT_BATCH)) == Counter({Zone.DUD: EXACT_BATCH})


def test_a_custom_mix_that_is_not_active_can_be_deleted(db_path: Path) -> None:
    """Deleted means gone from the switcher, not merely hidden."""
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)

    assert harness.core.delete_mix("duds only") is None
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


def test_deleting_the_active_mix_is_refused(db_path: Path) -> None:
    """Refused rather than allowed with a fallback. The draw does fall back to the first **Mix** there is,
    but a control that silently changed what the next **Batch** is made of is not a delete button — and
    switching away first is one click."""
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)
    harness.core.update_settings(active_mix="duds only")

    refused = harness.core.delete_mix("duds only")

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is SettingsRefused.Reason.MIX_IN_USE
    assert harness.core.active_mix() == Mix(name="duds only", unknown=0, banger=0, dud=100)


@pytest.mark.parametrize("name", ["explore", "refine"])
def test_the_two_named_mixes_cannot_be_deleted(db_path: Path, name: str) -> None:
    """**Explore** and **Refine** are the two the spec names. They are editable — the first test here
    edits one — but deleting one would leave the spec's own vocabulary with nothing behind it, and a
    database whose table can be emptied entirely has no **Mix** to fall back to."""
    harness = make_harness(db_path)
    harness.core.update_settings(active_mix="explore" if name == "refine" else "refine")

    refused = harness.core.delete_mix(name)

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is SettingsRefused.Reason.MIX_NOT_DELETABLE
    assert {mix.name for mix in harness.core.list_mixes()} == {"explore", "refine"}


def test_deleting_a_mix_nobody_has_heard_of_is_refused(db_path: Path) -> None:
    """A refusal and not a silent no-op, for the reason submitting twice is refused: the second tab needs
    to be told why nothing happened."""
    harness = make_harness(db_path)

    refused = harness.core.delete_mix("nope")

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is SettingsRefused.Reason.MIX_UNKNOWN
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


def test_a_deleted_mix_stays_deleted_across_a_restart(db_path: Path) -> None:
    first = make_harness(db_path)
    first.core.save_mix("duds only", unknown=0, banger=0, dud=100)
    first.core.delete_mix("duds only")

    assert make_harness(db_path).core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


# -- what is not a Mix ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "shares", "reason"),
    [
        ("thirds", (30, 30, 30), SettingsRefused.Reason.MIX_PERCENTAGES_INVALID),
        ("over", (60, 60, 60), SettingsRefused.Reason.MIX_PERCENTAGES_INVALID),
        ("negative", (110, -5, -5), SettingsRefused.Reason.MIX_PERCENTAGES_INVALID),
        ("fractional", ("33.3", "33.3", "33.4"), SettingsRefused.Reason.MIX_PERCENTAGES_INVALID),
        ("blank", ("", "50", "50"), SettingsRefused.Reason.MIX_PERCENTAGES_INVALID),
        ("", (50, 45, 5), SettingsRefused.Reason.MIX_NAME_INVALID),
        ("   ", (50, 45, 5), SettingsRefused.Reason.MIX_NAME_INVALID),
        ("x" * (MAX_MIX_NAME_LENGTH + 1), (50, 45, 5), SettingsRefused.Reason.MIX_NAME_INVALID),
    ],
)
def test_what_is_not_a_mix_is_refused_and_nothing_is_stored(
    db_path: Path, name: str, shares: tuple[int | str, int | str, int | str], reason: SettingsRefused.Reason
) -> None:
    """The acceptance criterion for validation, over every way of getting it wrong.

    Percentages that do not sum to 100 in either direction, a negative one, a fraction, a blank field, a
    name that is only whitespace and a name too long to be a button label. The rule is `validated_mix`'s
    and there is only one of it: the same function `list_mixes` drops a hand-edited row with.

    Nothing is stored either way — the refusal is checked *and* the table is checked, because a validator
    that refuses after writing would pass the first assertion on its own.
    """
    harness = make_harness(db_path)

    refused = harness.core.save_mix(name, unknown=shares[0], banger=shares[1], dud=shares[2])

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


def test_a_refused_edit_leaves_the_stored_mix_as_it_was(db_path: Path) -> None:
    """Refusing an edit to a **Mix** that exists must not half-apply it. **Explore** is still 75/20/5 and
    the draw still has something to read."""
    harness = make_harness(db_path)

    refused = harness.core.save_mix("explore", unknown=30, banger=30, dud=30)

    assert isinstance(refused, SettingsRefused)
    assert harness.core.active_mix() == EXPLORE_MIX


def test_a_name_is_trimmed_and_kept_as_it_was_typed(db_path: Path) -> None:
    """Stored as typed, trimmed, and compared case-sensitively.

    "Night" and "night" are two **Mixes** because the user typed two things; only the whitespace around
    them is the form's rather than the user's. Saving "  Night  " twice edits the one **Mix**, which is
    what makes the second save an edit rather than a duplicate the switcher would show twice.
    """
    harness = make_harness(db_path)

    harness.core.save_mix("  Night  ", unknown=10, banger=80, dud=10)
    harness.core.save_mix("night", unknown=20, banger=70, dud=10)
    harness.core.save_mix("Night", unknown=0, banger=90, dud=10)

    assert harness.core.list_mixes() == (
        Mix(name="Night", unknown=0, banger=90, dud=10),
        EXPLORE_MIX,
        Mix(name="night", unknown=20, banger=70, dud=10),
        REFINE_MIX,
    )
    assert harness.core.delete_mix(" night ") is None
    assert {mix.name for mix in harness.core.list_mixes()} == {"Night", "explore", "refine"}


def test_a_zone_at_zero_per_cent_is_a_mix(db_path: Path) -> None:
    """Allowed and meant: **Refine** without any **Duds** at all is a **Mix** somebody may well want, and
    a draw under it simply never allocates that **Zone** a **Slot**."""
    harness = make_harness(db_path)

    saved = harness.core.save_mix("no duds", unknown=30, banger=70, dud=0)

    assert saved == Mix(name="no duds", unknown=30, banger=70, dud=0)
