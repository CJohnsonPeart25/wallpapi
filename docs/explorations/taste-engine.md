# Taste engine: the room before the spec

Settled input for the specification session, from a boundary workshop on 2026-10-24. Everything under
**Settled** is the user's decision; treat it as given. Everything under **Open** is for the spec to answer.
This is a back-of-the-envelope picture of the room, not a design: no schema, no file formats, no code.

Supersedes the framing of `wallpapi-el0` (merging two installs' Decision logs): merging is one consequence of
the shape below, not a feature of its own.

## The room

A **taste engine**: it records judgements on any kind of item, distils them into a small portable model of
what one person likes, and finds new items in and around that space. Wallpapi becomes the first app on it.

Today it is one person moving between their own machines (home, work). Plan for a later platform where people
share and blend distillations ("anime" + "cowboy" gives the bangers in between; add "dark" on top), and for
items beyond wallpapers (local photos next; books, songs, animated backgrounds some day), without rebuilding
the foundations when the 16th instrument or the second app arrives.

The aim of the spec: decide the final, stable shape now, then build a small MVP that is a true slice of it.

## Words

Agreed in the workshop; the glossary (`CONTEXT.md`) is updated when the spec lands, not before.

- **Item**: anything judged. Identified by a namespaced key: `wallhaven:abc123`, `local:<hash>`. Wallhaven
  and Local are the two **Item sources** in scope.
- **Instrument**: one way of asking for a judgement: the verdict grid, star ratings, pick 1 of n, drag to
  order, an endless wall where you click out the interesting ones. Pluggable; there may be 16.
- **Judgement**: one raw event an Instrument records: what was shown, how, and what you did. A **Verdict**
  is one kind of Judgement. "Decision" stays avoided, as `CONTEXT.md` already says.
- **Evidence log**: the append-only record of Judgements, per install, mergeable. The diamond: the one thing
  that is never rewritten and from which everything else is rebuilt.
- **Space**: an embedding model that turns an Item into a vector. Every derived thing is stamped with the
  Space it came from.
- **Distillation**: the Evidence log compressed into a few weighted **prototypes** in one Space (several
  points, not one: the average of anime and cowboy is neither). Derived, rebuildable, portable.
- **Blend**: combining Distillations. **Lens**: a temporary steer applied at ranking time ("dark and moody"
  today, "light and airy" tomorrow); swapping it touches nothing stored.
- **Score** is the number an Item gets; **Ranking** is the order that comes out. Kept apart.

## Layers

```
 Surfaces      wallpapi (rotation, Library)  ·  analysis/viz  ·  future apps
 ────────────────────────────────────────────────────────────────────────────
 Ranking       candidates x (Distillation + Lens) -> order   <- today's scoring + Zones
 Lenses        Blend(a, b) · steer("dark and moody") · today's choice
 Distillation  Evidence log + Space -> weighted prototypes   (derived, stamped, portable)
 Evidence log  append-only Judgements, per install, mergeable
 Instruments   verdict grid · stars · pick 1 of n · drag to order · endless wall ...
 Space         Item -> vector, stamped with its model
 Item sources  wallhaven: · local: · later books:, songs: ...
```

Today's modules, roughly: `wallhaven`, `pool`, `thumbnails` are Item sources; `similarity` is a Space;
`decisions` is the Evidence log; `scoring`, `allocation` are Ranking. Distillation and Lenses do not exist.
Instruments are hard-wired as the four Verdicts. Library, Mix, Zone and Batch are wallpapi's own concepts,
above the engine.

## Settled

1. **One person, many installs** now; nothing in the format rules out many people later.
2. **Items are namespaced** (`source:id`); Wallhaven is the only source built, Local next.
3. **The atomic fact is a Judgement event**: the set shown, the Instrument, the responses. The shown set is
   evidence: a pick of 1 from 8 says something about the other 7.
4. **Design for every Instrument, build few**: the four Verdicts plus one comparative Instrument first.
5. **Engine and app split**: one repo, two packages. The engine knows nothing of Wallhaven or the Library.
6. **Every Instrument owns its reduction.** The log stores raw Judgements; each Instrument declares if and
   how its Judgements become evidence, of two kinds: **absolute** ("Item X, strength s") and **relative**
   ("A over B"). Stars reduce to absolute, ordering and picks to relative, an endless-wall click to a weak
   absolute plus weak "this over the ones passed".
7. **Weak signals are recorded and weighted later.** A Skip and scrolled-past Items are raw Judgements; their
   Instrument starts them at zero weight, tuned once there is data. Scrolling past counts only on a screen
   where you also acted (the presence rule): an untouched screen may mean you were away from the desk.
   ADR 0026 (a Skip teaches nothing) gains a note: a Skip is now recorded, still at zero weight.
8. **Mutation never rewrites the log.** Change happens by new Judgements or by a new reading of old ones
   (an Instrument's reduction, a Lens). Compaction exists only in exported snapshots.
9. **Order across installs**: each Judgement is identified by install plus local sequence, ordered across
   installs by recorded time with the install as tie-break. Latest wins, as locally today (Invariant 4);
   every Judgement is kept, so where installs disagree stays visible.
10. **Embeddings stay derived**, keyed by model as well as Item. Every derived output is stamped with the
    log position it was built to and the Space it used, so it can be deleted and rebuilt.
11. **Scores are still never stored** for live ranking (Invariant 2). Distillations and analysis results may
    be saved, as stamped caches.
12. **Distillation is a set of weighted prototypes**, and lives in one Space. Items of different media (images
    vs songs) are not promised to share a Space or a profile.
13. **Privacy line**: the Evidence log never leaves your machines unencrypted. A Distillation is the only
    thing that would ever go to a platform. A Distillation is lossy, not anonymous.
14. **Storage now** is an encrypted export file through a folder you already sync (or a private repo):
    append-only, safe to import twice. No server and no Supabase until a platform exists.
15. **Words as Lenses**: ADR 0013 relaxes so CLIP's text encoder can turn words into Lenses. Text is used
    for steering only, never as evidence.
16. **The data-science side is separate**: analysis and visualisation read an export, live in their own
    package or dependency group, and stay outside the app's locked stack. Offline first (notebook or
    generated report); the app gains a view only once a chart has earned it.
17. **Scale**: "a million" means "unbounded and cheap", not a literal target. The log and export are built
    for it; live ranking leans on the fixed-size Distillation rather than every past Judgement, which is the
    route past Invariant 2's Pool x decided matrix when that grows too big. Any change to Invariant 2
    needs an ADR and a measurement.

## Seams

The repo's seams are `typing.Protocol`s (`Clock`, `LibraryWriter`, `ModelSource`, `EmbeddingStore`), not
ABCs: structural, no inheritance, an in-memory fake satisfies one. The engine follows suit. A seam is real
when two adapters vary across it; the candidates and their two adapters today:

| Seam | Adapters that already vary | Its interface, roughly |
|---|---|---|
| Item source | Wallhaven, Local | Items and their thumbnails |
| Space | CLIP image encoder, the colour-and-category fallback | Items -> vectors, with a model stamp |
| Instrument | Verdict grid, Skip (then one comparative) | name, version, reduce(Judgement) -> evidence |
| Lens | words (text encoder), a Blend | adjust a Distillation for one ranking |

Distillation has one implementation, so it is a module, not a seam, until a second way to distil exists.

## Open: the basement (foundations the spec must answer)

- **How to tell a Distillation is good.** With no evaluation (hold back some Judgements, predict them),
  nothing can compare Instruments, reductions, or Distillation methods. Needed before any of them is tuned.
- **Model migration.** A new Space strands every Distillation built in the old one. Your own rebuild from
  the log; someone else's shared Distillation cannot. Does a Distillation expire, or carry its prototypes'
  source Items so it can be re-embedded?
- **Context at judgement time.** "Dark in the evening, light in the morning" chosen automatically needs
  context recorded with each Judgement (time of day exists; device, which install, maybe more). What is
  recorded now decides what can be learnt later.
- **Taste drift.** Is a Judgement from three years ago still you? Fading by log position is already a
  deferred decision; Distillation snapshots over time would make drift visible.
- **Your own noise.** Re-showing an Item months later measures how consistent you are, which bounds how
  good any model can get. It collides with "never show a decided Wallpaper again" (deferred).
- **Local photos are the most private data in the room** (faces, family). Their embeddings and Judgements
  never leave the machine, Distillation included? Decide before Local lands.
- **A Distillation can be inverted** into approximate images of what you like. Settled 13 says it is not
  anonymous; the platform question is what sharing one means.

## Open: the loft (out-there, worth a look, nothing depends on them yet)

- **Generate the bangers in between**: condition an image generator on a blended Distillation instead of
  searching for neighbours.
- **The engine chooses the question**: active learning picks the pair or set whose answer would teach the
  most, and the Instrument shows that.
- **Taste in words**: caption each prototype with the text encoder ("neon, rain, solitude"), so a
  Distillation explains itself and can be edited by hand.
- **Taste compatibility**: the distance between two Distillations, and what you share with someone.
- **Anti-taste**: a negative Distillation as a Lens that subtracts.
- **Context Lenses**: weather, calendar, time of day choosing today's Lens for the wallpaper rotation.

## Seats for the specification

- **Data engineer**: the Evidence log, the export, merging across installs.
- **Recommender/ML**: reductions, Distillation, Blends, Lenses, evaluation.
- **Domain modeler**: the glossary, the ADRs, the engine/app split and its seams.
- **Dataviz**: what is looked at, and where.
- **Security**: encryption, keys, what is ever shared.
