"""**Evidence**: what a **Judgement** says about Items, in the two kinds ranking reads.

Both kinds carry the id and recorded time of the **Judgement** they came from: the time decides which
absolute Evidence stands, and the id breaks an exact tie and lets the evaluation hold a whole **Judgement**
out. An Item is named by its key, namespaced by source (`wallhaven:<id>`); `taste/` never looks inside one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AbsoluteEvidence:
    """One Item judged on its own: a **Verdict** or a **Favourite**, as a signed strength. Resolves
    latest-wins per Item."""

    item: str
    strength: float
    judgement_id: str
    recorded_at: dt.datetime
    """UTC-aware."""


@dataclass(frozen=True, slots=True)
class RelativeEvidence:
    """One Item preferred over another on the same screen. Never resolved: every comparison counts,
    contradictions included. The strength is unitless."""

    winner: str
    loser: str
    strength: float
    judgement_id: str
    recorded_at: dt.datetime
    """UTC-aware."""


type Evidence = AbsoluteEvidence | RelativeEvidence
