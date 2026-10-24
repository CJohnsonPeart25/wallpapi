# Research: distilling evidence into weighted prototypes, and blending them

Ticket `wallpapi-wa5.6`, under the map `wallpapi-wa5`. Feeds the Distillation method ticket and the Lenses
and Blends ticket. Facts are cited to the source that owns them; lines marked **(inference)** are this
note's reasoning, not a source's claim. No decision is taken here.

## Answer

- **The standard recipe is "cluster what you liked, keep one point per cluster, weight each by how much and
  how recently".** Pinterest's PinnerSage does exactly this over fixed image embeddings: cluster a user's
  last 90 days of positive actions (Ward clustering), represent each cluster by one real member (the
  medoid), and weight it by a time-decayed count. It beat single-vector profiles clearly in production.
- **An Item is scored by its best-matching prototype, not by the average.** Every multi-interest system
  read here (PinnerSage, MaxMF, MIND, ComiRec) scores with "max over the user's vectors". Averaging
  disparate vectors lands in a different concept altogether; PinnerSage shows one.
- **Negatives can be repellers in three established ways**: subtract them (Rocchio, one vector, weighted
  low), keep negative prototypes beside positive ones (prototype classifiers, learning vector
  quantisation), or fit the prototypes so liked beats disliked (MaxMF's pairwise loss over fixed item
  vectors). Today's `scoring.py` is the keep-everything end of the same spectrum.
- **Relative evidence ("A over B") fits as Bradley-Terry in embedding space**: give each Item a score that is
  a function of its vector, and fit the function so winners outscore losers. Linear is a convex logistic
  regression on difference vectors; "max over prototypes" is MaxMF. Pick-1-of-n and drag-to-order extend it
  (Luce, Plackett-Luce).
- **Recency is usually an exponential weight**, `exp(-lambda * age)`, put on each piece of evidence or only
  on each prototype's weight. PinnerSage puts it on the weight only (lambda 0.01); Koren found that
  decaying old ratings *hurt* his models. The repo's deferred decision already says: half-life in log
  sequence, not time.
- **Blends**: the union of two Distillations gives "either"; the average gives "neither"; "between" is
  the **intersection** (score an Item by the *lower* of its two scores, or their product: each taste can
  veto), plus **midpoint prototypes** (normalised `a + b` for paired prototypes) to say where to look.
- **Cost is not the problem, except for one method.** Every method here is linear or near-linear in the
  evidence, except Ward-style hierarchical clustering, which needs memory quadratic in the Items clustered
  (about 400 MB at ten thousand, impossible at a million). Ranking against K prototypes is Pool x K x 512,
  independent of the log's length, which is Settled 17's route past Invariant 2.

## What the question covers

A log of absolute ("Item X, strength s") and relative ("A over B") evidence over Items in one fixed Space,
today CLIP ViT-B/32 with 512 dimensions, L2-normalised (`src/wallpapi/similarity.py`). Wanted: a small set
of weighted prototypes (the Distillation, Settled 12), and a way to combine two of them (a Blend) that finds
Items between two tastes rather than at their average.

Two facts about this Space matter throughout:

- On unit vectors, squared Euclidean distance is `2 - 2 * cosine`, so "nearest by Euclidean distance" and
  "most similar by cosine" pick the same neighbours. Methods stated in either form transfer.
- CLIP cosines between natural images are bunched: mapped into `[0, 1]` they spanned 0.59 to 1.0 in the
  repo's own spike (ADR 0013). Liang et al. trace this to deep networks' embeddings occupying a narrow cone,
  and show image and text embeddings sit apart in the shared space (the "modality gap") [Liang 2022].
  **(inference)** Any threshold or kernel width has to be tuned to this bunching, as ADR 0023's radius was.

## 1. Clustering the positives into prototypes

### PinnerSage (Pinterest, KDD 2020): the closest published match

PinnerSage assumes item embeddings are fixed and produced by a black-box model [PinnerSage §3], which is
this repo's situation with CLIP. Its three steps [PinnerSage §3.1, Algorithm 2]:

1. Take the user's action pins (repins and clicks) from the last 90 days and cluster them with **Ward**,
   hierarchical agglomerative clustering that merges the pair of clusters giving the least increase in
   within-cluster variance. A merge threshold `alpha` sets the number of clusters, so it varies per user:
   "a light user might get represented by 3-5 clusters, whereas a heavy user ... 75-100" [§2].
2. Represent each cluster by its **medoid**: the member minimising the summed squared distance to the
   others. Chosen over the centroid because a centroid "could lie in a very different region" when outliers
   join a cluster, and because a medoid is just an item id, so it caches [§3.1.2]. Medoids cut serving
   cost by 75% against centroids [Table 2]; on ranking quality, centroid and medoid tied [§5.3.2].
3. Weight each cluster by **importance** = `sum over members of exp(-lambda * (T_now - T_action))` [eq. 3].
   `lambda = 0` counts members (frequency); `lambda = 0.1` favours recent interests; 0.01 was best, and
   both 0 and 0.1 did worse [§3.1.3, Table 4].

Serving samples at most 3 medoids by importance and retrieves their nearest neighbours [§4]. For ranking,
candidates are ordered by "their maximum cosine similarity with any user embedding" [§5.3.2]. Negative
feedback is not used to build the clusters; impressions serve only as negatives in evaluation [§5.3].

Against single-vector baselines: a decay-weighted average of action embeddings lifted retrieval relevance
28% over "last pin"; PinnerSage lifted it 110%; replacing Ward with k-means (k=5) gave 91% [Table 4].
Figure 2 of the paper shows the averaging failure directly: merging three disparate pin embeddings yields
an embedding best described as a fourth, unrelated concept [§2]. That is Settled 12's "the average of anime
and cowboy is neither", measured.

### Choosing how many prototypes

| Source | Rule |
| --- | --- |
| PinnerSage | Ward merge threshold `alpha`; the count falls out of the data (3-5 to 75-100 per user) |
| MIND (Tmall) | `K_u = max(1, min(K, log2(n_u)))` for a user with `n_u` behaviours [MIND eq. 9] |
| MaxMF (Google) | Fixed `T`; gains grew with T = 2, 3, 5, 10 on YouTube and Google Music [MaxMF Tables 2-4] |
| ComiRec | Fixed `K`; the best K was 2, 4 or 6 depending on dataset and variant [ComiRec §4] |
| DP-means | A new cluster opens when a point is farther than `lambda` from every centre [Kulis 2012] |

DP-means is k-means with a penalty per cluster: it "is very similar to the classical k-means algorithm
except that a new cluster is formed whenever a point is sufficiently far away from all existing cluster
centroids", and it monotonically reaches a local optimum of that penalised objective [Kulis 2012 §1].
Allen et al. use it to give each class a set of prototypes whose number is inferred, and note the effect:
it "interpolate[s] between nearest neighbor and prototypical representations" [Allen 2019, abstract].

**(inference)** That spectrum names where today's code sits. `scoring.py` sums a kernel over every decided
Wallpaper within the Similarity radius: the nearest-neighbour end, which keeps everything and so grows with
the log. A Distillation moves toward the prototype end. DP-means' distance threshold plays the part the
Similarity radius plays now, so the radius has a natural successor.

On unit vectors, the usual k-means variant is **spherical k-means**: assign by cosine and renormalise each
centroid to unit length [Dhillon 2001 §1]. scikit-learn's `KMeans` takes a `sample_weight`, where a weight of
2 "is equivalent to adding a duplicate of that sample" [sklearn clustering guide]: the hook for evidence
strength and recency.

### What MIND and ComiRec add, and what they do not

Both are deep models trained across hundreds of thousands of users at once (ComiRec's Amazon Books set has
459,133 users [ComiRec Table 2]); they learn the item embeddings and the interest extractor together. With
one person and a fixed Space, the trainable parts have nothing to learn from. What carries over:

- **Score by the best-matching interest.** ComiRec's straightforward aggregation is
  `f(u, i) = max over k of e_i . v_u^k` [ComiRec eq. 11]; MIND found hard attention (the limit where the
  closest interest dominates) "converge faster and perform the best" [MIND §4].
- **Diversity at selection time, not in the profile.** ComiRec picks the final N greedily to maximise
  `sum f(u, i) + lambda * sum g(i, j)`, where `g` rewards dissimilar pairs [ComiRec eq. 12, Algorithm 2].
  **(inference)** This is a Ranking-layer concern, akin to today's Zones and Mix, not a Distillation one.

## 2. Negatives as repellers

Four established shapes, from least to most fitting:

1. **Rocchio** (relevance feedback in information retrieval): the new query is
   `alpha * q0 + beta * mean(relevant) - gamma * mean(non-relevant)`, with reasonable values
   `alpha = 1.0, beta = 0.75, gamma = 0.15`, negative components clipped to zero. "Positive feedback is much
   more valuable than negative" [Manning ch. 9, via the Cambridge course slides]. One vector, so it has
   the averaging failure above; the low `gamma` is the transferable lesson.
2. **Separate positive and negative prototypes, scored by distance.** Prototypical networks classify a
   point by a softmax over its distances to each class's prototype [Snell 2017 eq. 2]; Allen et al. give
   each class several prototypes [Allen 2019 §3]. Read "like" and "dislike" as two classes, each with its
   own clusters. **(inference)** A Distillation would then carry negative prototypes too, and an Item's
   score would compare its nearest liked prototype with its nearest disliked one.
3. **Learning vector quantisation** moves labelled prototypes with "attractive and repulsive forces": the
   nearest prototype of the right class toward a sample, the nearest of a wrong class away [GLVQ].
   Generalised LVQ makes this the gradient of a cost on `mu(x) = (d1 - d2) / (d1 + d2)`, with `d1` the
   distance to the nearest right-class prototype and `d2` to the nearest wrong-class one; it converges
   where plain LVQ may not [GLVQ eq. 4, 9-10]. Repelling, by name.
4. **Fit the prototypes so liked beats disliked**: MaxMF, in section 3.

**(inference)** Two cautions from the repo's own history apply to every shape. Negatives vastly outnumber
positives here (692 decided at ADR 0023's measurement: 407 Bans, 257 Ignores, 27 Likes, 1 Favourite), and
ADR 0023 found that spreading strong negatives grows the Dud zone. Rocchio's `gamma` well below `beta` is the
literature's version of the same lesson.

## 3. Relative evidence: Bradley-Terry in embedding space

**Bradley-Terry** gives each item a positive strength `pi_i` and sets
`P(i beats j) = pi_i / (pi_i + pi_j)`; writing `pi = exp(s)` makes it a logistic function of the score
difference `s_i - s_j` [Bradley 1952]. **Luce's** choice model extends it to picking one from a set,
`P(i from S) = pi_i / sum over j in S of pi_j`, and **Plackett-Luce** to full orderings; the `choix` library
implements all three (`*_pairwise`, `*_top1`, `*_rankings`) and regularises with a Gaussian prior whose
strength is `alpha` [choix docs]. **(inference)** Luce is the formal reading of Settled 3: a pick of 1 from
8 is evidence about all 8, through the shared denominator.

Classic Bradley-Terry fits one free strength per Item, so it says nothing about an Item never compared.
The embedding-space version makes the score a function of the Item's vector:

- **Linear.** Joachims' ranking SVM orders items by `w . phi(d)` and shows that learning `w` from pairwise
  preferences "is equivalent to that of a classification SVM on pairwise difference vectors"
  [Joachims 2002 eq. 15]. The logistic loss in place of the hinge gives Bradley-Terry with
  `s(x) = w . x`. Convex, one optimum, 512 parameters. One direction of taste only: it cannot like two
  separate regions while disliking the space between.
- **Max over prototypes.** MaxMF scores `f(u, d) = max over i of U_iu . V_d` with T vectors per user
  [MaxMF eq. 2] and trains with a pairwise hinge on (liked, not-liked) pairs. Its second phase fixes the
  item vectors and trains each user's T vectors alone [MaxMF Algorithm 2], which is the fixed-Space,
  one-person case exactly. A violating pair moves the liked item's best interest toward it and the
  other item's best interest away from that item [MaxMF eq. 3-4]: pairwise evidence acting as attraction
  and repulsion. Not convex, so the starting point matters.
- **Two-stage (inference).** Fix prototype locations by clustering, then fit only their weights from the
  pairs: with the per-prototype similarities as K fixed features, that is a K-parameter logistic
  regression, convex and small. It keeps clustering's interpretability and lets relative evidence set the
  weights. No source read here does this in exactly this form.

Absolute and relative evidence can share one fit by adding a pointwise loss to the pairwise one; Sculley's
"Combined Regression and Ranking" (KDD 2010) is the cited source for that, but its full text was not reached
for this note.

## 4. Recency weighting

| Where | How | Source |
| --- | --- | --- |
| Per evidence item, before fitting | `exp(-lambda * t)`; `lambda` per user and item cluster | Ding 2005 eq. 8 |
| Single-vector profile | `sum over actions of exp(-lambda * age) * P_a` | PinnerSage §3 |
| Per prototype weight only | members clustered unweighted; importance decays | PinnerSage eq. 3 |
| Hard window | last 90 days in batch; latest 20 actions online | PinnerSage §2, §4.2 |

Two cautions from the sources:

- **Koren (Netflix Prize, KDD 2009):** "prediction quality improves as we moderate that time decay,
  reaching best quality when there is no decay at all", because "underweighting past actions loses too much
  signal along with the lost noise" [Koren 2009]. He models drift explicitly with time-dependent terms
  instead. His setting is collaborative (many users); the warning about scarce per-user data still applies.
- **Half-life arithmetic.** For `exp(-lambda * t)` the half-life is `ln 2 / lambda`. PinnerSage's 0.01 is a
  half-life of about 69 time units (its ages are in days [§3, Fig. 3]). Ding and Li call `T0` a half-life
  but set `lambda = 1 / T0`, which leaves weight `e^-1`, about 0.37, at `T0` [Ding 2005 eq. 6-8].

**(inference)** PinnerSage's choice keeps an old interest as a prototype with a small weight rather than
dissolving it, which is what makes drift visible across Distillation snapshots (the map's Open item). The
repo's deferred decision ("a half-life in log sequence, not time") slots into either the per-evidence or
the per-prototype row unchanged.

## 5. Blends: between, not average

Combining several preference models is studied as group recommendation (combining individual models);
Masthoff's aggregation strategies include Average, Least Misery (the minimum of the individual scores),
Most Pleasure (the maximum) and Multiplicative [Masthoff, as summarised in the group-recommender tutorial
literature; the original was not reached for this note]. In probability terms, Hinton contrasts mixtures
with products: a mixture's distribution "cannot be sharper than the individual models in the mixture",
while a product of experts can be much sharper, because data that satisfies one expert but violates the
others "will be ruled out by their low probability under the other experts" [Hinton 2002 §1].

Mapping that onto two Distillations A and B with prototype scores `sA(x)`, `sB(x)`:

| Blend | Score | Finds |
| --- | --- | --- |
| Union (mixture, Most Pleasure) | `max(sA, sB)`, or pool both prototype sets | Items like A *or* like B |
| Average of vectors | one prototype at the mean | neither; the PinnerSage Figure 2 failure |
| Intersection (product, Least Misery) | `min(sA, sB)`, or `sA + sB` in log space | Items fit for *both* |
| Midpoint prototypes | new prototypes at normalised `a_i + b_j` | where to *look* for in-between Items |

**(inference)** "Anime + cowboy gives the bangers in between" is the intersection: an Item scores well only
if neither taste vetoes it. The intersection says what to keep, not where to search. If A and B are far
apart, few Items in a random candidate set score well on both, so the candidate source also needs
pointing between them. Midpoint prototypes do that: for unit vectors at the halfway point, the normalised
average `(a + b) / |a + b|` is exactly the spherical interpolation (slerp) midpoint. White found linear
interpolation in high-dimensional generative latents passes through low-norm, off-distribution points and
recommends slerp [White 2016 §2.1]; normalising fixes the norm, but nothing guarantees the midpoint of two
CLIP vectors is a meaningful picture. The midpoint is a search hint, and the intersection decides.

Two further facts for the Blend ticket:

- "Add 'dark' on top" is a third factor in the same product, or a Lens. CLIP text vectors sit apart from
  image vectors (the modality gap [Liang 2022]), so a text Lens compared by raw cosine with image
  prototypes is offset; it shifts every image by a similar amount, which matters for thresholds more than
  for ordering. **(inference)**
- Negatives under a Blend: in an intersection either taste's repellers should still veto; in a union, a
  repeller of A may be an attractor of B. The literature read here does not settle this.

## 6. Cost at ten thousand to a million Judgements

Back-of-envelope for one person on one machine; the arithmetic is this note's, the complexities are the
sources'. A 512-d float32 vector is 2 KB.

- **Judgements are not Items.** One pick-1-of-8 Judgement is one row of evidence touching 8 Items and
  implying 7 pairs. A million Judgements is at most a few million Items' embeddings (2 GB per million,
  float32) and a few million pairs. Embeddings are already derived and cached (Settled 10).
- **Reducing the log to per-Item weights** is one pass, linear in Judgements.
- **Ward / agglomerative clustering**: scipy's implementation is O(n^2) time and "all algorithms use O(n^2)
  memory" [scipy linkage docs]; scikit-learn calls it computationally expensive without connectivity
  constraints [sklearn clustering guide]. PinnerSage gives O(m^2) for its version [§3.1.1]. The condensed
  distance matrix in float64 is about 400 MB at 10,000 Items, about 40 GB at 100,000, about 4 TB at a
  million: Ward only works on a capped set, which is why PinnerSage clusters 90 days, not a lifetime.
- **k-means, spherical k-means, DP-means**: per iteration about `n * K * 512` multiply-adds. At a million
  Items and K = 32 that is about 1.6e10, seconds per iteration on a laptop CPU with numpy. Positives are a
  small fraction of decided Items (28 of 692 at ADR 0023's measurement), so clustering positives alone is
  far smaller still. MiniBatchKMeans "converges faster than KMeans, but the quality of the results is
  reduced" [sklearn clustering guide].
- **Pairwise fits**: all pairs is O(n^2), but sampling pairs and running stochastic gradient descent
  removes the super-linear dependence; Sculley trained in 0.2-0.3 CPU seconds per task (100,000 steps on
  a 2009 laptop), "up to 100 times faster" than SVM-light's RankSVM, with no observable loss in ranking
  quality [Sculley 2009]. The two-stage weight fit above has K parameters, so it is trivial.
- **Ranking**: scoring a Pool against K prototypes is `Pool * K * 512`: 1,322 x 32 x 512 is about 2e7, the
  same at any log length. Today's Pool x decided matrix at a million decided Wallpapers would be
  1,322 x 1,000,000 float32, about 5 GB, before the kernel. This is Settled 17's route past Invariant 2,
  and any change to Invariant 2 still needs an ADR and a measurement.

## Options and trade-offs for the deciding tickets

For the **Distillation method** ticket (a decision belongs there, not here):

- **A. PinnerSage-style**: cluster the positives, keep medoids, weight by decayed importance. Published and
  measured; no training; interpretable; prototypes are real Items. But it ignores negatives and relative
  evidence, and Ward caps how many Items it can cluster.
- **B. A plus negative prototypes**, clustered the same way. Repellers without training, close to today's
  signed scoring. But two thresholds to tune, and negatives dominate the counts.
- **C. A and B, then fit the prototype weights from pairs.** Uses relative evidence; convex and small. But
  two stages, and a fitted weight may go negative and needs a rule.
- **D. MaxMF or GLVQ**: fit the prototype positions to the evidence. Uses every kind of evidence directly.
  But non-convex, more hyperparameters, and the prototypes are no longer real Items.

Medoids in A to C bear on the **model migration** basement question: a medoid is an Item key, so a shared
Distillation could be re-embedded in a new Space; a fitted point (D) cannot. **(inference)**

For the **Lenses and Blends** ticket: union, intersection (min or product) and midpoint prototypes are the
three candidates; they compose (midpoints to search, intersection to score).

## Sources

- [PinnerSage] Pal et al., "PinnerSage: Multi-Modal User Embedding Framework for Recommendations at
  Pinterest", KDD 2020. <https://arxiv.org/abs/2007.03634>
- [MIND] Li et al., "Multi-Interest Network with Dynamic Routing for Recommendation at Tmall", CIKM 2019.
  <https://arxiv.org/abs/1904.08030>
- [ComiRec] Cen et al., "Controllable Multi-Interest Framework for Recommendation", KDD 2020.
  <https://arxiv.org/abs/2005.09347>
- [MaxMF] Weston, Weiss, Yee, "Nonlinear Latent Factorization by Embedding Multiple User Interests",
  RecSys 2013.
  <https://research.google/pubs/nonlinear-latent-factorization-by-embedding-multiple-user-interests/>
- [Kulis 2012] Kulis, Jordan, "Revisiting k-means: New Algorithms via Bayesian Nonparametrics", ICML 2012.
  <https://arxiv.org/abs/1111.0352>
- [Allen 2019] Allen et al., "Infinite Mixture Prototypes for Few-Shot Learning", ICML 2019.
  <https://arxiv.org/abs/1902.04552>
- [Snell 2017] Snell, Swersky, Zemel, "Prototypical Networks for Few-shot Learning", NeurIPS 2017.
  <https://arxiv.org/abs/1703.05175>
- [Dhillon 2001] Dhillon, Modha, "Concept Decompositions for Large Sparse Text Data Using Clustering",
  Machine Learning 42. <https://www.cs.utexas.edu/~inderjit/public_papers/concept_mlj.pdf>
- [GLVQ] Sato, Yamada, "Generalized Learning Vector Quantization", NeurIPS 1995.
  <https://papers.nips.cc/paper/1113-generalized-learning-vector-quantization>
- [Manning ch. 9] Manning, Raghavan, Schütze, *Introduction to Information Retrieval*, ch. 9 (Rocchio).
  The book site was unreachable; values checked in the Cambridge course slides based on it:
  <https://www.cl.cam.ac.uk/teaching/1718/InfoRtrv/slides/lecture7-relevance-feedback.pdf>
- [Bradley 1952] Bradley, Terry, "Rank Analysis of Incomplete Block Designs: I. The Method of Paired
  Comparisons", Biometrika 39 (3/4). Cited, not read; the formula is the standard statement.
- [choix docs] Maystre, `choix`: <https://github.com/lucasmaystre/choix>,
  <https://choix.lum.li/en/latest/api.html>
- [Joachims 2002] Joachims, "Optimizing Search Engines using Clickthrough Data", KDD 2002.
  <https://www.cs.cornell.edu/people/tj/publications/joachims_02c.pdf>
- [Sculley 2009] Sculley, "Large Scale Learning to Rank", NeurIPS 2009 workshop.
  <https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/35662.pdf>
- Sculley, "Combined Regression and Ranking", KDD 2010, doi:10.1145/1835804.1835928 (abstract only).
- [Koren 2009] Koren, "Collaborative Filtering with Temporal Dynamics", KDD 2009.
  <https://faculty.cc.gatech.edu/~zha/CSE8801/CF/kdd-fp074-koren.pdf>
- [Ding 2005] Ding, Li, "Time Weight Collaborative Filtering", CIKM 2005.
  <https://cseweb.ucsd.edu/classes/fa17/cse291-b/reading/p485-ding.pdf>
- [Hinton 2002] Hinton, "Training Products of Experts by Minimizing Contrastive Divergence", Neural
  Computation 14 (tech report GCNU TR 2000-004). <https://www.cs.toronto.edu/~hinton/absps/tr00-004.pdf>
- [White 2016] White, "Sampling Generative Networks". <https://arxiv.org/abs/1609.04468>
- [Liang 2022] Liang et al., "Mind the Gap: Understanding the Modality Gap in Multi-modal Contrastive
  Representation Learning", NeurIPS 2022. <https://arxiv.org/abs/2203.02053>
- [Masthoff] Masthoff, "Group Recommender Systems: Combining Individual Models", Recommender Systems
  Handbook, 2011. Not reached; strategy names as cited in the group-recommender literature.
- [scipy linkage docs]
  <https://docs.scipy.org/doc/scipy/reference/generated/scipy.cluster.hierarchy.linkage.html>
- [sklearn clustering guide] <https://scikit-learn.org/stable/modules/clustering.html>
- Repo: `src/wallpapi/scoring.py`, `src/wallpapi/similarity.py`, ADR 0013, ADR 0023; the exploration
  `docs/explorations/taste-engine.md` on `taste-engine-sketch`.
