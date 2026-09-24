# 2. The Batch persists until it is submitted, and there is no skip

Date: 2026-09-24

## Status

Accepted

## Context

The walking skeleton first minted a **Batch** on every `GET /`. Two minutes of real use left five **Batches**
stored against two submitted: each page load wrote a **Batch** row and its eight `batch_wallpapers` rows
whether or not anything was ever decided, and those rows have nothing to clean them up.

Worse than the rows, each load also silently rerolled the **Wallpapers** on screen, and each one is a
Wallhaven **API call** against the documented 45 per minute. A refresh is not a decision, and neither is
reopening a tab.

The obvious alternative reading is that a page load should simply *show* random images and only a decision
should be worth persisting. That cannot work as stated. Submit takes only a **Batch** ID, so the server has
to know what was shown; the **Draft Batch** at #3 is keyed `(batch_id, wallpaper_id)` and needs the **Batch**
to exist server-side; and `/thumb/{id}` resolves a **Wallpaper**'s thumbnail URL from storage, so tiles would
404 if nothing were stored. Something has to be written down before it can be decided on.

## Decision

`get_next_batch` returns the unsubmitted **Batch** if there is one, and searches Wallhaven only when there
isn't. At most one unsubmitted **Batch** exists at a time, and every stored **Wallpaper** either carries a
**Verdict** or belongs to that one **Batch**.

There is no skip. The only way past a **Batch** is to submit it, which records an **Ignore** for everything
unpicked.

Consequences that follow and are not optional:

- The check happens again inside the write transaction, so two tabs opened at once cannot each mint a
  **Batch**. The search stays outside it — holding the write lock across a network call would be a far worse
  trade than the duplicate it prevents.
- Storing a **Batch** is still not a **Verdict**. Nothing is appended to the **Decision log** until submit,
  so a **Wallpaper** is never written off for having merely appeared. `test_a_batch_that_is_never_submitted_records_nothing` pins this.
- One **API call** per **Batch**, not per page load.

## Consequences

Closing a tab and coming back resumes where you were rather than throwing the **Batch** away, which is the
behaviour you want from something you dip in and out of.

The decision is forced: there is no way forward that isn't a **Verdict**. That is the cost, and it is
accepted deliberately. If you want none of the eight, ignoring all eight says exactly that, and walking away
costs nothing because the **Batch** will still be there.

Anything that adds a skip later must not quietly become a reroll button. Held down, that is one **API call**
per click against a 45-per-minute budget, and it converts a tool for making decisions into a slot machine.

## Alternatives considered

**A fresh Batch per page load.** What was built first, and the naive reading of "show me some wallpapers".
Rejected once real use showed the accumulation and the wasted **API calls**, and because rerolling the
**Wallpapers** somebody is looking at because they pressed F5 is hostile.

**Persist nothing until submit, carrying the Batch in the form.** Rejected: it contradicts submit taking only
a **Batch** ID, leaves the #3 **Draft Batch** nowhere to hang, and breaks thumbnail serving. It also trusts
the browser about what was shown, which is the wrong place for that fact to live.

**A skip that discards the Batch without recording anything.** Rejected: the only motivation is finding the
decision hard, and **Ignore** already exists to express "none of these". It would also be the reroll button
described above.
