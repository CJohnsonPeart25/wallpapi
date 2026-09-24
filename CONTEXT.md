# wallpapi

A personal tool for finding wallpapers you actually like. It shows batches of Wallhaven images, learns your taste from the verdicts you give, and downloads the ones you love into a folder Windows rotates through. It never touches the desktop itself.

## Language

### The images

**Wallpaper**:
One Wallhaven image, identified by its Wallhaven ID.
_Avoid_: image, picture, background, wall

**Pool**:
The wallpapers fetched and stored locally that passed the filters and are eligible to appear in a batch.
_Avoid_: cache, candidates, queue

**Filters**:
The hard rules a wallpaper must satisfy before it enters the pool: minimum resolution, allowed ratios, SFW only, and minimum Wallhaven favourites. Distinct from scoring — filters exclude outright, scores only rank.
_Avoid_: criteria, constraints, preferences

**Batch**:
The n wallpapers shown at once. A pair is simply a batch of 2.
_Avoid_: page, set, round, grid

### Judging

**Verdict**:
A judgement on one wallpaper in a batch. There are four: favourite, like, ignore and ban.
_Avoid_: rating, vote, decision, choice

**Explicit Verdict**:
A favourite, like or ban — a verdict the user actively chose, as opposed to an ignore.
_Avoid_: manual verdict, active verdict

**Favourite**:
The strongest positive verdict. The wallpaper is downloaded into the library.
_Avoid_: star, keep, save

**Like**:
A weaker positive verdict, recorded in history but never downloaded.
_Avoid_: maybe, shortlist, upvote

**Ignore**:
The implicit, mildly negative verdict given to every wallpaper in a submitted batch that wasn't picked. Ignores stack, but only while the wallpaper has no explicit verdict.
_Avoid_: skip, pass, no-op

**Ban**:
The strongest negative verdict. The wallpaper is never shown again, and its weight spreads to similar wallpapers.
_Avoid_: block, hide, reject, dislike

**Verdict resolution**:
The rule that turns a wallpaper's verdicts into one value: the latest explicit verdict wins outright and all its ignores are disregarded; without one, ignores stack.
_Avoid_: aggregation, tallying

**Decision log**:
The append-only record of every verdict, history edit and clearance. It is the single source of truth.
_Avoid_: history table, audit log, events

**History**:
The view listing past verdicts, where any verdict can be changed or cleared. A view over the decision log, not a second store.
_Avoid_: log, activity, timeline

### Learning

**Score**:
A wallpaper's derived value, calculated from the decision log with each resolved value spread to similar wallpapers, fading with distance. It is never stored.
_Avoid_: rating, weight, rank, affinity

**Similarity provider**:
The component that measures how alike two wallpapers are. Its method is deliberately left open.
_Avoid_: embedder, model, comparator

**Zone**:
The category a pool wallpaper falls into: banger, dud or unknown.
_Avoid_: bucket, tier, band, class

**Banger**:
A wallpaper with a positive score.
_Avoid_: hit, winner, match, recommended

**Dud**:
A wallpaper with a negative score that isn't banned.
_Avoid_: miss, reject, bad

**Unknown**:
A wallpaper with no decided wallpaper within the similarity radius, or with a score of exactly zero.
_Avoid_: new, unseen, undecided

### Building a batch

**Mix**:
The zone percentages used to build a batch.
_Avoid_: ratio, blend, profile, strategy

**Explore**:
The default mix for casting a wide net — mostly unknowns.
_Avoid_: discovery mode, wide mode

**Refine**:
The default mix for narrowing down — mostly bangers.
_Avoid_: focus mode, exploit mode

**Allocation**:
Turning a mix into slots for a batch: whole-number slots are guaranteed, and leftover slots are rolled by the fractional remainders.
_Avoid_: distribution, sampling, apportioning

**Revisit weight**:
The setting that reduces how often a wallpaper with an explicit verdict reappears in a batch.
_Avoid_: cooldown, decay, penalty

### Output

**Library**:
The output folder of favourites, written one way only and never read back. Windows' own personalisation settings handle rotation from it.
_Avoid_: downloads, collection, gallery, output dir

**Core service**:
The single interface between the UI and everything else, and the only seam tests enter through.
_Avoid_: engine, manager, API, backend
