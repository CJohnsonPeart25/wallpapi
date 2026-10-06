# 22. Lookalike turns go by groups of similar subjects

Date: 2026-10-06

## Status

Accepted. Partly supersedes ADR 0005: its "every **Favourite** gets a turn before any gets a second" and its
"a like: walk picks one **Favourite** at a time" paragraph. A like: walk is now about one subject, a
**Favourite** or a **Like**, and turns go by group. The strict random/like alternation, the three-page cap,
the `relevance` sort, the like: walk keeping its place at target, failure handling and `source = 'like'`
stand as ADR 0005 has them.

## Context

ADR 0005 rotated the **Lookalike search** through the **Favourites**, so that every one had a turn before any
had a second. A taste therefore got turns in proportion to how many **Favourites** it had. With 40 mountain
**Favourites** and 5 neon cities, neon got about one like: walk in nine. The **Banger** zone grew mostly with
mountains, and the minority taste was starved before scoring ever saw it.

Only **Favourites** were subjects. The **Decision log** examined for #38 held 12 **Likes** and 0
**Favourites** (27 and 1 by October), so the like: strategy had never run there.

wallpapi-58 looks at the same density bias at the **Banger** draw. It is parked, and shares no mechanism
with this decision: this ADR is the refill-side half.

## Decision

**A lookalike subject is any Wallpaper whose resolved Verdict is Favourite or Like.**
`decisions.lookalike_subjects` reads them through **Verdict resolution** on every step (invariant 4), so a
subject re-decided as **Ban** or **Ignore**, or cleared, leaves the rotation by itself, mid-walk if need be.
A **Like** has the same standing as a **Favourite**. The grouping below is what keeps a heavily liked taste
from crowding out the rest, so no weight between the two is needed.

**The subjects are grouped by similarity, and turns go by group.** `pool.Refill` is handed the
**Similarity provider**. Each time it takes up a new like: walk, and only then, it calls
`similarities(subjects, subjects)` and groups the result with `pool.similar_groups`. That is one decided x
decided matrix (invariant 2). The read, the call and the grouping all run outside the Refill's lock, because
the provider runs SQL. Nothing is stored: the groups are made again for the next walk, so a new subject or
a changed setting counts at once.

**Grouping is greedy leader clustering at the Similarity radius.** In id order, each subject joins the first
leader it is within `similarity_radius` of, by the **Score**'s distance `1 - similarity`, or leads a new
group. Connected components were measured and rejected: near neighbours chain, and at 0.15 they put 24 of
the live log's 28 subjects in one group. A leader's group is at most twice the radius across. The radius is
the setting scoring uses, with no constant of its own, so a retune (wallpapi-52) moves both together.

**The group whose latest turn is oldest goes next; within it, the subject walked longest ago.** The Refill
counts its like: steps and remembers, for each current subject, the step at which its latest walk began. A
group never walked counts as oldest, and so does a subject never walked within its group. So a new taste is
asked about at the next walk, and every subject in a group has a turn before any has a second. Ties go to
the Refill's own seeded random source.

## Consequences

Two tastes get alternate like: walks however unequal their numbers. A taste with one subject gets as many
walks as a taste with forty, and that one subject is walked forty times as often as each of the forty. This
is the intent: the refill asks about tastes, and scoring weighs them.

The grouping is only as good as the **Similarity provider**. On the colours-and-category fallback, groups
are coarse. Once the model is open, an unembedded subject scores 0 against the rest (wallpapi-55), so it is
a group of its own and gets turns as one until it is embedded.

A like: walk's group is decided when the walk starts. A **History** edit that would regroup the subjects
changes nothing until the next walk is taken up, at most three like: steps away.

The grouping costs one subjects x subjects matrix per walk taken up: tens of subjects, once every few
steps.

## Alternatives considered

**Connected components at the radius.** Simple, and order-free. Rejected, because they chain: on the live
log, 24 of 28 subjects formed one group.

**Clustering into a fixed number of groups (k-means, as ADR 0018 draws the Unknowns).** It would need a k,
and no number of tastes is right for every log. The radius already says how alike two **Wallpapers** must
be to count as the same taste.

**Weighting a subject's turns by the inverse of its group's size.** The same balance, but it needs a random
draw per walk and gives no guarantee that a taste is asked about soon. Strict turns by group need no number.

**Keeping Likes out, or giving them a smaller share.** A log of **Likes** alone would never search like: at
all. Grouping already balances tastes, so a second weight would have nothing to correct.
