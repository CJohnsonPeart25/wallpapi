# wallpapi

A personal tool for finding wallpapers you actually like. It shows batches of Wallhaven images, learns your taste from the verdicts you give, and downloads the ones you love into a folder Windows rotates through. It never touches the desktop itself.

## Language

### The images

**Wallpaper**:
One Wallhaven image, identified by its Wallhaven ID.
_Avoid_: image, picture, background, wall

**Pool**:
The wallpapers fetched and stored locally that passed the filters, have never been decided, and are eligible to appear in a batch. Deciding a wallpaper retires it: submitting a batch takes everything it showed out of the pool, ignores included, and the refill never lets a wallpaper the decision log mentions back in. A retired wallpaper still counts towards every score.
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
How many wallpapers the refill keeps waiting in the pool. Below it the refill spends its whole budget; at or above it the refill idles. Every submitted batch takes the pool below it again, so the pool is a stream rather than a backlog, and 500 by default is enough to draw from while keeping every whole-pool calculation cheap. Lowering it never trims the pool: one above target drains as batches are submitted. A setting.
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
The mildly negative verdict given to every wallpaper in a submitted batch left unmarked. Seen once, done: like every verdict it retires the wallpaper from the pool, so it is not shown again. Unlike a ban it spreads only an ignore's weight, and history can overturn it. Ignores do not stack: passing a wallpaper over twice, once in a batch and once from history, counts the same as once.
_Avoid_: skip, pass, no-op

**Ban**:
The strongest negative verdict. The wallpaper is never shown again, and its weight spreads to similar wallpapers.
_Avoid_: block, hide, reject, dislike

**Draft Batch**:
The verdicts marked against a batch that hasn't been submitted yet. Held against the batch, replaced outright rather than toggled, and discarded on submit. It is not part of the decision log.
_Avoid_: pending verdicts, staged verdicts, selection, basket

**Verdict resolution**:
The rule that turns a wallpaper's decision log entries into one value: the latest entry decides, whatever it is, and nothing before it counts. An ignore after a like overturns the like; a like after an ignore overturns the ignore.
_Avoid_: aggregation, tallying

**Decision log**:
The append-only record of every verdict and history edit. It is the single source of truth.
_Avoid_: history table, audit log, events

**Clearance** (legacy):
A decision log entry that withdrew a wallpaper's explicit verdict, leaving it as if never seen. No longer made — withdrawing a verdict is an ignore — but old ones remain in the log, and a wallpaper whose latest entry is a clearance resolves to nothing.
_Avoid_: undo, reset, delete, revert

**History**:
The view listing past verdicts, where any verdict can be changed to any other, ignore included. A view over the decision log, not a second store, and the only way to revisit a decision: a batch never shows a decided wallpaper again, and changing its verdict here does not put it back in the pool.
_Avoid_: log, activity, timeline

### Learning

**Score**:
A wallpaper's derived value, calculated from the decision log with each resolved value spread to similar wallpapers, fading with distance. It is never stored.
_Avoid_: rating, weight, rank, affinity

**Similarity provider**:
The component that measures how alike wallpapers are. It is asked for a whole matrix at once — every pool wallpaper against every decided one — and never about a single pair. It is a CLIP image encoder run locally over the cached thumbnails, with the colour-and-category measure as the fallback for anything not yet embedded. Still called the similarity provider whichever one is running — the word names the seam, not the method behind it.
_Avoid_: embedder, model, comparator, CLIP

**Embedding**:
The few hundred numbers a CLIP image encoder turns one thumbnail into, standing for what is in the picture. Cached permanently, computed in the background, and never stored in the decision log's database. Two wallpapers of the same thing have close embeddings whatever their colours, which is the whole reason the provider changed.
_Avoid_: vector, feature, encoding, latent

**Tag** (legacy):
One of Wallhaven's own labels on a wallpaper, applied by its users. Only used by an opt-in similarity provider that lost to embeddings and is due to go; nothing else reads them.
_Avoid_: label, keyword, category

**Similarity radius**:
How far a verdict reaches, as a distance from 0 to 1. Beyond it a decided wallpaper counts for nothing at all, which is what makes a wallpaper with nothing decided nearby an unknown. A setting.
_Avoid_: threshold, cutoff, neighbourhood

**Similarity decay**:
How fast a verdict fades with distance inside the radius. The radius is a cliff; the decay is the slope up to it. A setting.
_Avoid_: falloff, gamma, damping

**Zone**:
The category a pool wallpaper falls into: banger, dud or unknown. Only undecided wallpapers have one, because only undecided wallpapers are in the pool.
_Avoid_: bucket, tier, band, class

**Banger**:
An undecided wallpaper with a positive score — a prediction about something not yet shown, never a past favourite.
_Avoid_: hit, winner, match, recommended

**Dud**:
An undecided wallpaper with a negative score.
_Avoid_: miss, reject, bad

**Unknown**:
An undecided wallpaper with no decided wallpaper within the similarity radius, or with a score of exactly zero.
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

### Output

**Library**:
The output folder of favourites, written one way only. Windows' own personalisation settings handle rotation from it. wallpapi never lists it and never reads a file back out of it; the one thing it ever asks the folder is whether a path it recorded itself is still there, and it asks that only when the favourites are downloaded. Everything wallpapi writes into it is named for its wallpaper — a Wallhaven ID and one short extension — and nothing outside it is ever written or deleted, whatever a recorded path says.
_Avoid_: downloads, collection, gallery, output dir

**Confinement**:
The guarantee that the only folder wallpapi can write into or delete from is the library folder currently configured. A path is confined when its name is one wallpapi could have chosen and, once fully resolved — dot-dot collapsed, every symlink and junction followed — it lies strictly inside the resolved library folder. Both the write and the deletion go through the same check, and a recorded path that fails it is dropped from wallpapi's records with the file left exactly where it is.
_Avoid_: sandbox, jail, validation, sanitising

**Favourite download**:
Writing a library file for every favourite that has not got one, on demand rather than at submission. The one-way half of library reconciliation, and the only place wallpapi looks at the folder: a favourite counts as missing its file when there is no recorded path, when the recorded path is not on the disk, or when it no longer resolves inside the library folder. It never deletes, so it is the repair for a library deleted in Explorer, moved, or arriving on a new machine with the decision log.
_Avoid_: sync, restore, rebuild, repair

**Library reconciliation**:
Making the library agree with the decision log: every favourite with no file gets one, and every file whose wallpaper is no longer a favourite loses it. The library is derived from the decision log rather than written as a side effect of a click, so reconciling twice does nothing the second time. It runs after every submission and reads nothing but the log — what the folder actually holds is a question only a favourite download asks.
_Avoid_: sync, download queue, flush, refresh

**Thumbnail cache**:
The locally stored thumbnails wallpapi serves to its own pages, so batches never hotlink Wallhaven. Distinct from the library: it holds thumbnails, not full-resolution favourites — one for every pool member, fetched in the background before it is ever shown, as well as for anything shown or decided. It is also what the CLIP image encoder embeds, so the whole pool has embeddings and not only what has been on screen. A thumbnail is kept for as long as any page might show it — for ever, once the wallpaper has an explicit verdict, because history renders one. The background fetching holds off while the cache is at its size cap.
_Avoid_: image cache, thumbs, local store, static files

**Core service**:
The single interface between the UI and everything else, today. It is being split into modules with seams of their own, and tests enter through those, not through it.
_Avoid_: engine, manager, API, backend

### Configuration

**Settings**:
Everything the user configures, persisted between sessions and read as one typed value from the core service. Batch size, the library path, the filters, the pool target size, the similarity radius and decay, the thumbnail cache limit and the active mix. The mixes themselves are rows of their own, not a setting — the active one is the setting that names which.
_Avoid_: config, preferences, options

**Batch size**:
How many wallpapers a batch holds. A setting, read when a batch is minted, so changing it applies to the next batch rather than the one on screen.
_Avoid_: n, page size, grid size

**Library path**:
Where the library folder is. A setting, always an absolute path, and not necessarily a folder that exists yet. It defines confinement, so changing it changes what wallpapi may touch: files already written elsewhere are neither moved nor deleted, and a favourite download is what fills the new folder.
_Avoid_: output folder, download directory, destination
