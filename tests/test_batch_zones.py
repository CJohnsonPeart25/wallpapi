"""The **Zone** a **Batch** shows each **Wallpaper** from. Issue #9.

Recorded against the **Batch** at mint time rather than worked out again when the page is rendered. Those
are different facts: what the draw used, and what the **Decision log** says now. The tile has to show the
first, or the label would change under the user as they marked the **Batch** in front of them.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch
from wallpapi.model import Verdict, Zone
from wallpapi.web.app import create_app

CATALOGUE_SIZE = 24


def _everything_resembles(decided_id: str) -> dict[tuple[str, str], float]:
    """Hand-defined similarities making every **Wallpaper** in the catalogue look like one of them.

    Near enough to be well inside the default radius, so a single **Favourite** turns the whole **Pool**
    into **Bangers** — including the seven that picked up the derived **Ignore** the same submission wrote,
    whose own -10 at distance 0 is nowhere near enough to cancel a +100 at 0.05.
    """
    return {(f"wp{n:04d}", decided_id): 0.95 for n in range(CATALOGUE_SIZE)}


def test_every_wallpaper_in_a_batch_comes_with_the_zone_it_was_drawn_from(db_path: Path) -> None:
    """The acceptance criterion, at the seam: a **Zone** per **Wallpaper**, for every **Wallpaper** shown."""
    harness = make_harness(db_path, catalogue=catalogue_of(CATALOGUE_SIZE))

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert {w.id for w in batch.wallpapers} == set(batch.zones)
    assert set(batch.zones.values()) == {Zone.UNKNOWN}


def test_a_batch_keeps_the_zones_it_was_minted_with_across_a_reload(db_path: Path) -> None:
    """The **Batch** persists until it is submitted (ADR 0002), and so do its **Zones**.

    Read back off storage rather than held in the process, so the second page load says what the first one
    did. It is the same reason the **Draft Batch** is stored: a reload must not change what is on screen.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(CATALOGUE_SIZE))
    minted = harness.core.get_next_batch()
    assert isinstance(minted, Batch)

    reloaded = harness.core.get_next_batch()

    assert isinstance(reloaded, Batch)
    assert reloaded.id == minted.id
    assert reloaded.zones == minted.zones


def test_the_zones_recorded_are_the_ones_the_pool_was_classified_into(db_path: Path) -> None:
    """The **Batch** shows what the classification said, not a second opinion.

    A **Favourite** is submitted, and everything similar to it is a **Banger** the moment the next
    **Batch** is minted. The **Zones** on that **Batch** have to agree with `classify_pool` exactly —
    if they could drift apart, the label on the tile would be decoration rather than a fact.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(CATALOGUE_SIZE))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    loved = first.wallpapers[0].id
    harness.similarity.similarity_by_pair.update(_everything_resembles(loved))
    harness.core.set_draft_verdict(first.id, loved, Verdict.FAVOURITE)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    classified = {scored.wallpaper.id: scored.zone for scored in harness.core.classify_pool()}
    assert following.zones == {w.id: classified[w.id] for w in following.wallpapers}
    assert set(following.zones.values()) == {Zone.BANGER}


def test_a_banned_wallpaper_is_never_drawn_into_a_batch(db_path: Path) -> None:
    """**Banned** means in no **Zone**, and a **Batch** is drawn from the **Zones**.

    #3 already excluded **Bans** from the draw; what this pins is that routing the draw through the
    classification has not quietly reopened the hole. There is no **Zone** for a **Banned** **Wallpaper**
    to be recorded under, which is the structural version of the same guarantee.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(CATALOGUE_SIZE))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    banned = first.wallpapers[0].id
    harness.core.set_draft_verdict(first.id, banned, Verdict.BAN)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    assert banned not in following.zones
    assert banned not in {w.id for w in following.wallpapers}


def test_the_batch_page_shows_each_tile_its_zone(db_path: Path) -> None:
    """The acceptance criterion as the user meets it: the word, on the tile.

    `data-zone` carries the glossary term so the stylesheet and anything later can key off it, and the same
    word is the tile's visible text — three states told apart only by a colour would be three states
    nobody colour-blind can tell apart.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(CATALOGUE_SIZE))
    app = create_app(harness.core)

    with TestClient(app) as client:
        first = client.get("/batch")
        batch = harness.core.get_next_batch()
        assert isinstance(batch, Batch)
        loved = batch.wallpapers[0].id
        harness.similarity.similarity_by_pair.update(_everything_resembles(loved))
        client.post("/draft", data={"batch_id": batch.id, "wallpaper_id": loved, "verdict": "favourite"})
        client.post("/submit", data={"batch_id": batch.id})
        second = client.get("/batch")

    assert 'data-zone="unknown"' in first.text
    assert 'data-zone="banger"' in second.text
    assert ">banger<" in second.text


def test_a_tile_swapped_back_after_a_mark_still_carries_its_zone(db_path: Path) -> None:
    """The htmx swap re-renders one tile on its own, so the **Zone** has to be in that context too.

    Easy to lose: the tile is included by the grid on a page load and rendered alone by `/draft`, and only
    the first of those gets its variables from the loop. A mark that silently dropped the label would look
    like the **Zone** had changed because the **Wallpaper** was marked.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(CATALOGUE_SIZE))
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch = harness.core.get_next_batch()
        assert isinstance(batch, Batch)
        swapped = client.post(
            "/draft",
            data={"batch_id": batch.id, "wallpaper_id": batch.wallpapers[0].id, "verdict": "like"},
        )

    assert 'data-zone="unknown"' in swapped.text
