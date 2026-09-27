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
The hard rules a wallpaper must satisfy before it enters the pool: minimum resolution, allowed ratios, SFW only, and minimum Wallhaven favourites. Distinct from scoring — filters exclude outright, scores only rank. Checked locally on the way in, whether or not the search already asked for them.
_Avoid_: criteria, constraints, preferences

**Refill**:
The background work that keeps the pool stocked: Wallhaven searches, filtered, at up to 45 API calls a minute while the pool is below its target size. A thread, started with the app and stopped with it, and the only thing in wallpapi that searches Wallhaven.
_Avoid_: fetcher, crawler, scraper, sync

**Refill strategy**:
Which search a refill step makes, and so how a pool member got there: random, or lookalikes. The two take strict turns while both have work, so neither starves the other.
_Avoid_: mode, channel, feed, phase

**Lookalike search**:
A search for the wallpapers Wallhaven considers similar to one favourite. The refill strategy that grows the banger zone, as against the random one that stocks the unknown zone. Wallhaven spells it `like:` and its results are few, so a walk through them is capped.
_Avoid_: similar search, related, recommendations, more like this

**Pool target size**:
How many wallpapers the refill keeps waiting in the pool. Below it the refill spends its whole budget; at or above it the refill idles. A setting.
_Avoid_: quota, capacity, limit, threshold

**Walk**:
One continuous sweep of search pages for one refill strategy. A random walk carries the seed Wallhaven returned so its pages do not repeat each other, and ends when the pool reaches its target or a page comes back empty; the next one starts from a fresh seed. A lookalike walk is about one favourite and ends at an empty page or its page cap, after which the next favourite has its turn. The two walks keep their places separately.
_Avoid_: crawl, scan, sweep, pass

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

**Draft Batch**:
The verdicts marked against a batch that hasn't been submitted yet. Held against the batch, replaced outright rather than toggled, and discarded on submit. It is not part of the decision log.
_Avoid_: pending verdicts, staged verdicts, selection, basket

**Verdict resolution**:
The rule that turns a wallpaper's decision log entries into one value: the latest entry that is not an ignore decides. An explicit verdict counts alone and every ignore on the wallpaper is disregarded, before it and after it alike; a clearance, or no such entry, lets every ignore stack.
_Avoid_: aggregation, tallying

**Decision log**:
The append-only record of every verdict, history edit and clearance. It is the single source of truth.
_Avoid_: history table, audit log, events

**Clearance**:
The decision log entry that removes a wallpaper's explicit verdict, after which its ignores stack again. A clearance is an entry in its own right, not a verdict.
_Avoid_: undo, reset, delete, revert

**History**:
The view listing past verdicts, where any verdict can be changed or cleared. A view over the decision log, not a second store.
_Avoid_: log, activity, timeline

### Learning

**Score**:
A wallpaper's derived value, calculated from the decision log with each resolved value spread to similar wallpapers, fading with distance. It is never stored.
_Avoid_: rating, weight, rank, affinity

**Similarity provider**:
The component that measures how alike wallpapers are. Its method is deliberately left open. It is asked for a whole matrix at once — every pool wallpaper against every decided one — and never about a single pair.
_Avoid_: embedder, model, comparator

**Similarity radius**:
How far a verdict reaches, as a distance from 0 to 1. Beyond it a decided wallpaper counts for nothing at all, which is what makes a wallpaper with nothing decided nearby an unknown. A setting.
_Avoid_: threshold, cutoff, neighbourhood

**Similarity decay**:
How fast a verdict fades with distance inside the radius. The radius is a cliff; the decay is the slope up to it. A setting.
_Avoid_: falloff, gamma, damping

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
The zone percentages used to build a batch. Three whole numbers summing to a hundred, under a name. Stored rather than written into the code, so they can be edited and added to. A mix is identified by its name: saving one under a name that already exists edits that mix, and saving it under a new name makes another.
_Avoid_: ratio, blend, profile, strategy

**Active mix**:
The mix the next batch will be built from. A setting, read when a batch is minted, so switching applies to the next batch rather than the one on screen.
_Avoid_: current mix, selected mix, mode

**Explore**:
The default mix for casting a wide net — mostly unknowns. Its percentages can be edited; it cannot be deleted.
_Avoid_: discovery mode, wide mode

**Refine**:
The default mix for narrowing down — mostly bangers. Editable and, like explore, permanent.
_Avoid_: focus mode, exploit mode

**Allocation**:
Turning a mix into slots for a batch: whole-number slots are guaranteed, and leftover slots are rolled by the fractional remainders.
_Avoid_: distribution, sampling, apportioning

**Slot**:
One place in a batch, allocated to a zone before anything is drawn into it. A slot has a zone it was meant for; the wallpaper that ends up in it may have come from another.
_Avoid_: seat, position, pick

**Shortfall**:
The slots a zone was allocated and cannot fill, because it holds fewer eligible wallpapers than it was asked for. They are filled from unknown, then banger, then dud — so a decision log with no favourites in it yet gives an all-unknown batch. A shortfall is ordinary, not an error: the tile still shows the zone its wallpaper came from, never the zone its slot wanted.
_Avoid_: deficit, underfill, fallback

**Revisit weight**:
The setting that reduces how often a wallpaper with an explicit verdict reappears in a batch.
_Avoid_: cooldown, decay, penalty

### Output

**Library**:
The output folder of favourites, written one way only and never read back. Windows' own personalisation settings handle rotation from it.
_Avoid_: downloads, collection, gallery, output dir

**Library reconciliation**:
Making the library agree with the decision log: every favourite with no file gets one, and every file whose wallpaper is no longer a favourite loses it. The library is derived from the decision log rather than written as a side effect of a click, so reconciling twice does nothing the second time.
_Avoid_: sync, download queue, flush, refresh

**Thumbnail cache**:
The locally stored thumbnails wallpapi serves to its own pages, so batches never hotlink Wallhaven. Distinct from the library: it holds thumbnails for any wallpaper that has been shown or decided, not full-resolution favourites. A thumbnail is kept for as long as any page might show it — for ever, once the wallpaper has an explicit verdict, because history renders one.
_Avoid_: image cache, thumbs, local store, static files

**Core service**:
The single interface between the UI and everything else, and the only seam tests enter through.
_Avoid_: engine, manager, API, backend

### Configuration

**Settings**:
Everything the user configures, persisted between sessions and read as one typed value from the core service. Batch size, the library path, the filters, the pool target size, the similarity radius and decay, and the active mix today; the revisit weight joins them later. The mixes themselves are rows of their own, not a setting — the active one is the setting that names which.
_Avoid_: config, preferences, options

**Batch size**:
How many wallpapers a batch holds. A setting, read when a batch is minted, so changing it applies to the next batch rather than the one on screen.
_Avoid_: n, page size, grid size

**Library path**:
Where the library folder is. A setting, always an absolute path, and not necessarily a folder that exists yet.
_Avoid_: output folder, download directory, destination
