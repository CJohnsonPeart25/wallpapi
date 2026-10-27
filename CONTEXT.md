# wallpapi

A personal tool for finding wallpapers you actually like. It shows batches and line-ups of Wallhaven images, learns your taste from what you judge, and downloads the ones you love into a folder Windows rotates through. It never touches the desktop itself.

## Language

### The images

**Wallpaper**:
One Wallhaven image, identified by its Wallhaven ID. The item the Wallhaven item source supplies: wallpapi's own tables keep the bare ID, and the evidence log the namespaced key.
_Avoid_: image, picture, background, wall

**Item**:
Anything judged, identified by a key namespaced by its item source: `wallhaven:<id>`. A wallpaper is an item. The taste engine knows only items and their keys; what an item is (its URLs, colours, thumbnail) stays with wallpapi.
_Avoid_: object, entity, thing, asset

**Item source**:
Where items come from, named by the prefix of their keys. Wallhaven is the only one built. Another source arrives as a new prefix, not as a change to the evidence log.
_Avoid_: provider, feed, origin, backend

**Pool**:
The wallpapers fetched and stored locally that passed the filters, have never been decided, and are eligible to appear in a batch or a line-up. A wallpaper is decided once a verdict or a favourite has been recorded for it, a legacy clearance included, and deciding retires it: submitting a batch takes everything it showed out of the pool, ignores included, and the refill never lets a decided wallpaper back in. Being shown is not deciding: a line-up leaves its wallpapers in the pool, except any given a favourite, and so does a skipped batch. A retired wallpaper still counts towards every score.
_Avoid_: cache, candidates, queue

**Filters**:
The hard rules a wallpaper must satisfy before it enters the pool: minimum resolution, allowed ratios, SFW only, and minimum Wallhaven favourites. Distinct from scoring — filters exclude outright, scores only rank. Checked locally on the way in, whether or not the search already asked for them.
_Avoid_: criteria, constraints, preferences

**Refill**:
The background work that keeps the pool stocked: Wallhaven searches, filtered, at up to the API budget while the pool is below its target size. A thread, started with the app and stopped with it, and the only thing in wallpapi that searches Wallhaven.
_Avoid_: fetcher, crawler, scraper, sync

**Refill strategy**:
Which search a refill step makes, and so how a pool member got there: random, or lookalikes. The two take strict turns while both have work, so neither starves the other.
_Avoid_: mode, channel, feed, phase

**Lookalike search**:
A search for the wallpapers Wallhaven considers similar to one favourite or like; turns go by groups of similar subjects, so each taste is asked about in turn however many favourites and likes it holds. The refill strategy that grows the banger zone, as against the random one that stocks the unknown zone. Wallhaven spells it `like:` and its results are few, so a walk through them is capped.
_Avoid_: similar search, related, recommendations, more like this

**Pool target size**:
How many wallpapers the refill keeps waiting in the pool. Below it the refill spends its whole budget; at or above it the refill idles. Every submitted batch takes the pool below it again, so the pool is a stream rather than a backlog, and 500 by default is enough to draw from while keeping every whole-pool calculation cheap. Lowering it never trims the pool: one above target drains as batches are submitted, and a pool reset is how to start again at the lower size. A setting.
_Avoid_: quota, capacity, limit, threshold

**Pool reset**:
Emptying the pool and discarding the batch and the line-up on screen together, so the refill restocks from nothing to the pool target size. Nothing is decided by it and nothing is recorded: the evidence log, and so every score and the library, carries over, and a wallpaper it purged may be fetched into the pool again. A submitted batch is never touched. Lower the pool target size first to restock smaller.
_Avoid_: purge, wipe, flush, restart

**API budget**:
How many Wallhaven API calls a minute the refill may make: 35 by default, never more than Wallhaven's own 45. Wallhaven counts its 45 per IP address, across everything on the machine, so the budget leaves the rest for other callers. The refill also holds back for a minute when Wallhaven's own count of calls remaining says other traffic has used that rest. Thumbnail and full-resolution fetches are not API calls and do not spend it. A setting.
_Avoid_: rate limit, quota, throttle

**Walk**:
One continuous sweep of search pages for one refill strategy. A random walk carries the seed Wallhaven returned so its pages do not repeat each other, and ends when the pool reaches its target or a page comes back empty; the next one starts from a fresh seed. A lookalike walk is about one subject, a favourite or like, and ends at an empty page or its page cap, after which the next group has its turn. The two walks keep their places separately.
_Avoid_: crawl, scan, sweep, pass

**Batch**:
The n wallpapers shown at once on the verdict grid, each answered with a verdict or a favourite. A pair is simply a batch of 2.
_Avoid_: page, set, round, grid, line-up

### Judging

**Verdict**:
What you say about one wallpaper on the verdict grid or in history. There are three: like, ignore and ban. A favourite is not one: it is an act on any tile.
_Avoid_: rating, vote, decision, choice

**Explicit Verdict**:
A favourite, like or ban — a standing the user actively chose, as opposed to an ignore. A favourite counts as one, although it is no longer a verdict.
_Avoid_: manual verdict, active verdict

**Favourite**:
An act on any tile of any instrument, given from the star on it or with F: the wallpaper goes into the library, and it is the strongest positive. Not a verdict: it does not move the screen on, and it is recorded with the rest of the screen's judgement. On the verdict grid it is exclusive with a like or ban on the same tile. It retires the wallpaper from the pool, and a later like, ignore or ban from history overturns it, as one verdict overturns another.
_Avoid_: star, keep, save

**Like**:
A weaker positive verdict, recorded in history but never downloaded.
_Avoid_: maybe, shortlist, upvote

**Ignore**:
The mildly negative verdict given to every wallpaper in a submitted batch left unmarked. Seen once, done: like every verdict it retires the wallpaper from the pool, so it is not shown again. Unlike a ban it spreads only an ignore's weight, and history can overturn it. Ignores do not stack: passing a wallpaper over twice, once in a batch and once from history, counts the same as once.
_Avoid_: skip, pass, no-op

**Ban**:
The strongest negative verdict. The wallpaper is never shown again, and neither is a near-duplicate of it (the same image reposted under another ID). Its weight spreads to similar wallpapers.
_Avoid_: block, hide, reject, dislike

**Skip**:
Passing over the batch on screen without deciding anything. It is recorded as a judgement with every response blank, which its reduction gives zero weight; then it and its draft batch are thrown away and a new batch is drawn. Not a verdict and not an ignore: the skipped wallpapers stay in the pool, teach nothing, and may be drawn again — the best bangers straight away, since a skip changes no score.
_Avoid_: reroll, reset, pass, dismiss

**Draft Batch**:
The marks against a batch that hasn't been submitted yet: a verdict or a favourite per tile, and which tiles' previews were opened. Held against the batch, replaced outright rather than toggled, and discarded on submit, skip or pool reset. It is not part of the evidence log.
_Avoid_: pending verdicts, staged verdicts, selection, basket

**Verdict resolution**:
The rule that turns an item's absolute evidence into one standing: the latest recorded judgement decides, whatever it is, and nothing before it counts. Verdicts and favourite resolve together, and an exact tie in recorded time falls to the lower judgement id. An ignore after a like overturns the like; a like after an ignore overturns the ignore. Relative evidence is never resolved.
_Avoid_: aggregation, tallying

**History**:
The view listing every wallpaper decided, where its verdict or favourite can be changed to any other, ignore included. A view over the evidence log, not a second store: an edit is recorded as a judgement of that one wallpaper on the history screen, so changing your mind is a new fact and never a rewrite. The only way to revisit a decision: a batch never shows a decided wallpaper again, and changing its verdict here does not put it back in the pool.
_Avoid_: log, activity, timeline

### Recording

**Evidence log**:
The append-only record of every judgement, one per screen answered. It is the single source of truth: every score, the library and history are derived from it. Nothing in it is updated or deleted; a change of mind is a new judgement, and verdict resolution reads the latest recorded time. The decision log was converted into it once and frozen.
_Avoid_: decision log, history table, audit log, events

**Judgement**:
One screen answered, recorded whole: what was shown, how it was asked, what you did, and when. It carries the instrument and its version, the screen, the shown set, the response, each item's provenance, the ranking stamp, when the screen was first shown, when it was submitted (the recorded time) with the local UTC offset then, and a context of named fields, whose first is the page's theme, light or dark. Identified by a hash of its content, so the same judgement has the same id wherever it is kept. A verdict is part of a judgement's response, not a judgement of its own.
_Avoid_: decision, event, entry, rating

**Screen**:
Where a judgement was asked, named by the app: batch, history or compare.
_Avoid_: surface, page, view, tab

**Shown set**:
The items on screen when a judgement was made, in on-screen order: a batch's tiles, a line-up, or the one wallpaper a history edit changes. Item keys only. Always recorded, whether or not the instrument's reduction reads the neighbours as evidence.
_Avoid_: selection, candidates, set, page

**Response**:
What you did on a judgement's screen, in the shape its instrument gives it: fields per item (a verdict, a favourite, a preview opened, a rank) and optional fields for the whole judgement (none of these, a streak). The evidence log keeps it and never reads it; only a reduction does.
_Avoid_: result, input, reaction, outcome

**Provenance**:
Why each item in a shown set was there, as labels the taste engine keeps and never interprets. wallpapi records the zone it was drawn from, the zone its slot was meant for, and the mix, so that a later analysis can correct for the mix choosing what you saw.
_Avoid_: source, origin, reason, tags

**Ranking stamp**:
The ranking a shown set was drawn under: the log position ranking read (the newest judgement's recorded time and id), the space, and the similarity radius and decay, with empty places for a distillation and a lens. Enough to replay a past screen as that day's ranking saw it. A stamp, not stored scores, and absent when nothing ranked the set.
_Avoid_: snapshot, model version, score cache

**Instrument**:
One way of asking for a judgement, with a name and a version: the verdict grid, which asks for a verdict or a favourite per tile, and pick, order and gauntlet on the line-up. Its version is the format of its response. Each has its own reduction and evaluation reading.
_Avoid_: mode, tool, widget, question type

**Reduction**:
The plain function that turns a judgement into evidence, one per instrument name and version, held in one table in the taste engine. On the verdict grid a favourite is 100, a like 50, an ignore −10 and a ban −100; a skip and a preview opened give nothing. A changed weight is a new reduction of the same log, never a rewrite of it.
_Avoid_: reading, interpretation, weighting, scoring

**Evidence**:
What a reduction makes of a judgement, and all that ranking and the evaluation read. Absolute or relative, each piece carrying its judgement's id and recorded time.
_Avoid_: signal, feedback, label

**Absolute evidence**:
Evidence about one item on its own: the item and a strength on the verdicts' scale, from a verdict or a favourite. Resolved per item by verdict resolution. The only evidence ranking reads.
_Avoid_: rating, score, value

**Relative evidence**:
Evidence that one item beat another: a winner, a loser and a unitless strength, from pick, order or gauntlet. Never resolved: every comparison counts, contradictions included. The evaluation tests it; it reaches ranking only once the evaluation shows it helps.
_Avoid_: preference, duel, vote

**Decision log** (legacy):
The per-wallpaper record of every verdict and history edit that the evidence log replaced. Converted once, each submitted batch into one judgement and each history edit into a judgement of one wallpaper, every time kept; the old table is then frozen, unread. The heading stays so the ADRs that name it still resolve: read it as the evidence log.
_Avoid_: as a name for the evidence log

**Clearance** (legacy):
A decision log entry that withdrew a wallpaper's explicit verdict, leaving it as if never seen. No longer made — withdrawing a verdict is an ignore — but old ones are converted with the rest: as the latest judgement on a wallpaper a clearance still wins and adds nothing, so the wallpaper resolves to nothing, and it still counts as decided.
_Avoid_: undo, reset, delete, revert

### Comparing

**Line-up**:
The n wallpapers shown side by side on the compare tab to be compared, n a setting from 2 to 8, 4 by default. Drawn by the active mix and wear, from the pool minus the live batch's tiles, and kept until answered, so a reload shows the same one. The mix allocates its slots as for a batch; wear decides which wallpaper fills each, and an instrument may add its own rule, as gauntlet keeps its winner. Answering it takes nothing out of the pool except a wallpaper given a favourite.
_Avoid_: batch, comparison, matchup, grid

**Wear**:
How many times a wallpaper was on a line-up that was answered, "none of these" included, read from the evidence log as each line-up is drawn and never stored. Each showing makes a wallpaper wait longer to be lined up again: a banger is drawn by its score divided by one plus its wear, and an unknown or a dud fewest wear first. A line-up a pool reset discarded adds none, and a batch neither adds nor reads it. A stand-in for showing the comparison that teaches the most.
_Avoid_: exposure, views, impressions, fatigue

**Compare tab**:
The tab that holds the line-up, with pick, order and gauntlet as a choice on it. The batches tab stays the verdict grid.
_Avoid_: compare page, versus, arena

**Pick**:
The instrument that asks for the best of a line-up: one click or its number key, and the next line-up comes at once. None of these (0) says nothing. The winner beats each other tile.
_Avoid_: choose, select, best-of

**Order**:
The instrument that asks for a line-up in order of preference: click the tiles in turn, and click a ranked one to take it back. Ranking all but the last submits; Enter submits a partial order. A ranked tile beats every lower-ranked and every unranked one; unranked tiles say nothing about each other.
_Avoid_: sort, ranking, drag to order

**Gauntlet**:
The instrument where the winner stays: pick the best of the line-up, it keeps its place, the other tiles are replaced, and a crown counts its streak. None of these replaces every tile and ends the streak. The winner beats that screen's challengers.
_Avoid_: king of the hill, tournament, champion mode

### Learning

**Score**:
A wallpaper's derived value, calculated from the absolute evidence in the evidence log with each resolved value spread to similar wallpapers, fading with distance. It is never stored.
_Avoid_: rating, weight, rank, affinity

**Space**:
What places an item for similarity, named in every ranking stamp: the CLIP model's id, or the colour-and-category fallback. A different model is a different space, and nothing derived in one is compared with another.
_Avoid_: model, embedding space, vector space

**Similarity provider**:
The component that measures how alike wallpapers are. It is asked for a whole matrix at once — every pool wallpaper against every decided one — and never about a single pair. It is a CLIP image encoder run locally over the cached thumbnails, with the colour-and-category measure as the fallback for anything not yet embedded until the model is ready. After that an unembedded wallpaper counts for nothing: it is an Unknown, and as a decided wallpaper it moves no score. Still called the similarity provider whichever one is running — the word names the seam, not the method behind it.
_Avoid_: embedder, model, comparator, CLIP

**Embedding**:
The few hundred numbers a CLIP image encoder turns one thumbnail into, standing for what is in the picture. Cached permanently, computed in the background, and never stored in the evidence log's database. Two wallpapers of the same thing have close embeddings whatever their colours, which is the whole reason the provider changed.
_Avoid_: vector, feature, encoding, latent

**Similarity radius**:
How far a verdict or favourite reaches, as a distance from 0 to 1. Beyond it a decided wallpaper counts for nothing at all, which is what makes a wallpaper with nothing decided nearby an unknown. A setting.
_Avoid_: threshold, cutoff, neighbourhood

**Similarity decay**:
How fast a verdict or favourite fades with distance inside the radius. The radius is a cliff; the decay is the slope up to it. A setting.
_Avoid_: falloff, gamma, damping

**Zone**:
The category a pool wallpaper falls into: banger, dud or unknown. Only undecided wallpapers have one, because only undecided wallpapers are in the pool. A near-duplicate of a ban has none: it stays in the pool but is never drawn.
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

### Evaluating

**Evaluation reading**:
An instrument's frozen, versioned answer key for the evaluation, separate from its reduction. Version 1 reads the verdict grid as ban below ignore below like below favourite, as pairs between items of one shown set that stand differently, and a line-up as the pairs its reduction gives. A skip, a preview opened and a history edit are never test answers, and an answer is what was said then, never a later edit.
_Avoid_: exam, ground truth, label

**Pairwise accuracy**:
The evaluation's one number: of the pairs shown together and treated differently, the share the score orders the way you did. Ties count half and random is 0.5, and each judgement weighs once whatever its size. Given per instrument, with how many test judgements it rests on and a 95% range, by replaying the evidence log in order and predicting each judgement only from those before it.
_Avoid_: precision, hit rate, AUC

### Building a batch

**Mix**:
The zone percentages used to build a batch or a line-up. Three whole numbers summing to a hundred, under a name. Stored rather than written into the code, so they can be edited and added to. A mix is identified by its name: saving one under a name that already exists edits that mix, and saving it under a new name makes another.
_Avoid_: ratio, blend, profile, strategy

**Active mix**:
The mix the next batch or line-up will be built from. A setting, read when a batch or line-up is minted, so switching applies to the next one rather than the one on screen.
_Avoid_: current mix, selected mix, mode

**Explore**:
The default mix for casting a wide net — mostly unknowns. Its percentages can be edited; it cannot be deleted.
_Avoid_: discovery mode, wide mode

**Refine**:
The default mix for narrowing down — mostly bangers. Editable and, like explore, permanent.
_Avoid_: focus mode, exploit mode

**Allocation**:
Turning a mix into slots for a batch or a line-up: whole-number slots are guaranteed, and leftover slots are rolled by the fractional remainders.
_Avoid_: distribution, sampling, apportioning

**Slot**:
One place in a batch or a line-up, allocated to a zone before anything is drawn into it. A slot has a zone it was meant for; the wallpaper that ends up in it may have come from another.
_Avoid_: seat, position, pick

**Shortfall**:
The slots a zone was allocated and cannot fill, because it holds fewer eligible wallpapers than it was asked for. They are filled from unknown, then banger, then dud — so an evidence log with no favourites in it yet gives an all-unknown batch. A shortfall is ordinary, not an error: the tile still shows the zone its wallpaper came from, never the zone its slot wanted.
_Avoid_: deficit, underfill, fallback

### Output

**Library**:
The output folder of favourites, written one way only. Windows' own personalisation settings handle rotation from it. wallpapi never lists it and never reads a file back out of it; the one thing it ever asks the folder is whether a path it recorded itself is still there, and it asks that only when the favourites are downloaded. Everything wallpapi writes into it is named for its wallpaper — a Wallhaven ID and one short extension — and nothing outside it is ever written or deleted, whatever a recorded path says.
_Avoid_: downloads, collection, gallery, output dir

**Confinement**:
The guarantee that the only folder wallpapi can write into or delete from is the library folder currently configured. A path is confined when its name is one wallpapi could have chosen and, once fully resolved — dot-dot collapsed, every symlink and junction followed — it lies strictly inside the resolved library folder. Both the write and the deletion go through the same check, and a recorded path that fails it is dropped from wallpapi's records with the file left exactly where it is.
_Avoid_: sandbox, jail, validation, sanitising

**Favourite download**:
Writing a library file for every favourite that has not got one, on demand rather than at submission. The one-way half of library reconciliation, and the only place wallpapi looks at the folder: a favourite counts as missing its file when there is no recorded path, when the recorded path is not on the disk, or when it no longer resolves inside the library folder. It never deletes, so it is the repair for a library deleted in Explorer, moved, or arriving on a new machine with the evidence log.
_Avoid_: sync, restore, rebuild, repair

**Library reconciliation**:
Making the library agree with the evidence log: every favourite with no file gets one, and every file whose wallpaper is no longer a favourite loses it. The library is derived from the evidence log rather than written as a side effect of a click, so reconciling twice does nothing the second time. It runs after every judgement that can change a favourite — a batch submission, a history edit, a line-up answered with a favourite in it — and reads nothing but the log; what the folder actually holds is a question only a favourite download asks.
_Avoid_: sync, download queue, flush, refresh

**Thumbnail cache**:
The locally stored thumbnails wallpapi serves to its own pages, so batches never hotlink Wallhaven. Distinct from the library: it holds thumbnails, not full-resolution favourites — one for every pool member, fetched in the background before it is ever shown, as well as for anything shown or decided. It is also what the CLIP image encoder embeds, so the whole pool has embeddings and not only what has been on screen. A thumbnail is kept for as long as any page might show it — for ever, once the wallpaper has an explicit verdict, because history renders one. The background fetching holds off while the cache is at its size cap.
_Avoid_: image cache, thumbs, local store, static files

**Core service**:
Legacy term. It was the single interface between the UI and everything else until 2026-10-03, and is now the modules, `compose.py` and `workflows.py`; the heading stays so the ADRs that name it still resolve.
_Avoid_: engine, manager, API, backend

### Configuration

**Settings**:
Everything the user configures, persisted between sessions and read as one typed value from the settings module. Batch size, the line-up's n, the library path, the filters, the pool target size, the similarity radius and decay, the thumbnail cache limit and the active mix. The mixes themselves are rows of their own, not a setting — the active one is the setting that names which.
_Avoid_: config, preferences, options

**Batch size**:
How many wallpapers a batch holds. A setting, read when a batch is minted, so changing it applies to the next batch rather than the one on screen.
_Avoid_: n, page size, grid size

**Library path**:
Where the library folder is. A setting, always an absolute path, and not necessarily a folder that exists yet. It defines confinement, so changing it changes what wallpapi may touch: files already written elsewhere are neither moved nor deleted, and a favourite download is what fills the new folder.
_Avoid_: output folder, download directory, destination

### The taste engine and the app

**Taste engine**:
The judging and learning maths, kept as code only in a `taste/` folder inside wallpapi: data in, data out. It opens no file or connection, starts no thread, makes no network call, stores nothing, and imports nothing from the rest of wallpapi. It holds the judgement's shape, the reductions and evidence, verdict resolution with its example cases, the score maths, the CLIP maths (preprocessing, the embed step, similarity, the near-duplicate threshold), the evaluation, and the clock and seeded random source that wallpapi imports from it.
_Avoid_: engine, core, recommender, package

**App**:
Everything in wallpapi outside the taste engine, which hosts it and conforms to its data shapes: the item sources, every table, every SQL query and every migration, the threads and the network, the model download, the embedding cache and its upkeep, the colour-and-category fallback, the zone rule (the sign of a score), and wallpapi's own words — pool, batch, line-up, mix, allocation, library. `compose.py` stays the only place that knows the whole graph.
_Avoid_: host, platform, frontend
