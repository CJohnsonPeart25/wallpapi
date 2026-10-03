"""The **Filters** and the **Pool** target size, as settings. Issue #6.

**Filters** are the hard rules a **Wallpaper** must satisfy before it enters the **Pool**. Four of them are
configurable — minimum width, minimum height, allowed ratios and minimum **Favourites** — and two are not:
purity is fixed to SFW and every category stays on.

They are settings and nothing more here: what they exclude is `test_pool.py`. Everything enters through the
Core service, so what is asserted is the `Settings` value that comes back out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, make_harness
from wallpapi.core import (
    DEFAULT_ALLOWED_RATIOS,
    DEFAULT_MIN_WIDTH,
    DEFAULT_POOL_TARGET_SIZE,
    MAX_POOL_TARGET_SIZE,
    WALLHAVEN_RATIOS,
    Settings,
    SettingsRefused,
)


def test_the_filters_are_configurable_and_persist_across_a_restart(db_path: Path) -> None:
    """Acceptance criterion: minimum resolution, allowed ratios and minimum favourites are persisted.

    A second Core service over the same file, because "persisted" means surviving the process and not
    merely surviving the call.
    """
    first = make_harness(db_path)
    saved = first.core.update_settings(
        min_width=1920, min_height=1080, allowed_ratios="21x9,32x9", min_favourites=250
    )

    assert isinstance(saved, Settings)
    assert saved.min_width == 1920
    assert saved.allowed_ratios == ("21x9", "32x9")

    restarted = make_harness(db_path)
    settings = restarted.core.get_settings()

    assert settings.min_width == 1920
    assert settings.min_height == 1080
    assert settings.allowed_ratios == ("21x9", "32x9")
    assert settings.min_favourites == 250


def test_the_pool_target_size_is_configurable(harness: Harness) -> None:
    """Acceptance criterion: the **Pool** target size is a setting."""
    saved = harness.core.update_settings(pool_target_size=50)

    assert isinstance(saved, Settings)
    assert saved.pool_target_size == 50
    assert harness.core.get_settings().pool_target_size == 50


def test_updating_one_filter_leaves_the_others_alone(harness: Harness) -> None:
    """The calling convention ADR 0004 settled: `None` means "leave this one alone".

    The settings page grows sections, and a form that renders half of them must not reset the other half by
    omission.
    """
    harness.core.update_settings(min_width=1920, min_favourites=99)

    harness.core.update_settings(batch_size=4)

    settings = harness.core.get_settings()
    assert settings.batch_size == 4
    assert settings.min_width == 1920
    assert settings.min_favourites == 99


@pytest.mark.parametrize("bad", ["0", "-1", "two thousand", "1.5", ""])
def test_a_pool_target_size_that_is_not_a_whole_positive_number_is_refused(
    harness: Harness, bad: str
) -> None:
    """A **Pool** of none is a **Batch** page with nothing on it and no way off, exactly as a **Batch**
    size of none would be."""
    result = harness.core.update_settings(pool_target_size=bad)

    assert isinstance(result, SettingsRefused)
    assert result.reason is SettingsRefused.Reason.POOL_TARGET_SIZE_INVALID
    assert harness.core.get_settings().pool_target_size == DEFAULT_POOL_TARGET_SIZE


def test_a_pool_target_size_above_the_cap_is_refused(harness: Harness) -> None:
    """There is a ceiling because invariant 2 sizes the **Similarity provider**'s matrix off the **Pool**.

    Every whole-**Pool** scan — **Scoring** at #9, the **Filter** prune here — is linear in it, so an
    unbounded target is a system that gets slower for ever and never says why.
    """
    result = harness.core.update_settings(pool_target_size=MAX_POOL_TARGET_SIZE + 1)

    assert isinstance(result, SettingsRefused)
    assert result.reason is SettingsRefused.Reason.POOL_TARGET_SIZE_INVALID


@pytest.mark.parametrize("bad", ["-1", "wide", "2560.5", ""])
def test_a_minimum_resolution_that_is_not_a_whole_number_of_pixels_is_refused(
    harness: Harness, bad: str
) -> None:
    """Zero is allowed — "no minimum" is a real answer — but a negative or a fraction is a typo."""
    assert isinstance(harness.core.update_settings(min_width=bad), SettingsRefused)
    assert isinstance(harness.core.update_settings(min_height=bad), SettingsRefused)
    assert harness.core.get_settings().min_width == DEFAULT_MIN_WIDTH


def test_a_minimum_resolution_larger_than_any_display_is_refused(harness: Harness) -> None:
    """A minimum above the largest resolution Wallhaven serves can only be a typo, and its effect would be
    an empty **Pool** with no error anywhere to explain it."""
    result = harness.core.update_settings(min_width=100_000)

    assert isinstance(result, SettingsRefused)
    assert result.reason is SettingsRefused.Reason.MIN_WIDTH_INVALID


def test_zero_is_an_accepted_minimum_resolution(harness: Harness) -> None:
    """Turning a **Filter** off is done by setting it to nothing, not by a second "enabled" setting."""
    saved = harness.core.update_settings(min_width=0, min_height=0, min_favourites=0)

    assert isinstance(saved, Settings)
    assert saved.min_width == 0
    assert saved.min_height == 0
    assert saved.min_favourites == 0


@pytest.mark.parametrize("bad", ["-1", "lots", "1e3"])
def test_a_minimum_favourites_that_is_not_a_whole_number_is_refused(harness: Harness, bad: str) -> None:
    result = harness.core.update_settings(min_favourites=bad)

    assert isinstance(result, SettingsRefused)
    assert result.reason is SettingsRefused.Reason.MIN_FAVOURITES_INVALID


@pytest.mark.parametrize("bad", ["", "   ", "16:9", "16x9,widescreen", "4000x3000", ","])
def test_allowed_ratios_are_checked_against_wallhaven_s_own_list(harness: Harness, bad: str) -> None:
    """Validated against the ratios Wallhaven accepts, not merely against being a string.

    An unrecognised `ratios=` value is not an error Wallhaven reports — it is a search that quietly returns
    something other than what was asked for, which is the worst shape a bad setting can take. Empty is
    refused too: "every ratio" is written as the full list, because a blank field reads as a mistake.
    """
    result = harness.core.update_settings(allowed_ratios=bad)

    assert isinstance(result, SettingsRefused)
    assert result.reason is SettingsRefused.Reason.ALLOWED_RATIOS_INVALID
    assert harness.core.get_settings().allowed_ratios == DEFAULT_ALLOWED_RATIOS


def test_allowed_ratios_are_tidied_rather_than_rejected_for_whitespace(harness: Harness) -> None:
    """ "16x9, 16x10" is what a person types. Storing it tidy means the query string is tidy too."""
    saved = harness.core.update_settings(allowed_ratios=" 16x9 , 16x10 ,16x9 ")

    assert isinstance(saved, Settings)
    assert saved.allowed_ratios == ("16x9", "16x10")


def test_every_ratio_wallhaven_documents_is_accepted(harness: Harness) -> None:
    """The accepted list is Wallhaven's, pinned so that narrowing it is a deliberate edit."""
    saved = harness.core.update_settings(allowed_ratios=",".join(sorted(WALLHAVEN_RATIOS)))

    assert isinstance(saved, Settings)
    assert set(saved.allowed_ratios) == WALLHAVEN_RATIOS


def test_a_refused_filter_writes_nothing_at_all(harness: Harness) -> None:
    """Everything is validated before anything is written, so a good value beside a bad one does not
    half-apply and then get reported as a failure."""
    result = harness.core.update_settings(min_width=1920, allowed_ratios="nonsense")

    assert isinstance(result, SettingsRefused)
    assert harness.core.get_settings().min_width == DEFAULT_MIN_WIDTH
