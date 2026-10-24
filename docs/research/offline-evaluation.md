# Research: offline evaluation for a one-person taste model

Ticket `wallpapi-wa5.5`, for the evaluation-protocol ticket under the map `wallpapi-wa5`. Researched
2026-10-24 against the papers and library docs listed under Sources. Facts are cited by number, e.g. [3].
Where a decision is implied, this gives options and trade-offs; the decision belongs to the protocol ticket.
The back-of-the-envelope numbers in section 4 are my own arithmetic from standard formulas, assumptions
stated.

## Answer

- **Hold-out design: replay the log in order.** Take the Evidence log in sequence; before each Judgement
  (or each block of them), build the model from everything earlier and predict that Judgement; then add it
  and move on. This is *prequential* evaluation [5, 6]. Every Judgement gets used as a test case once, which
  matters at a few hundred Judgements, nothing from the future leaks in [3, 4], and plotting the running
  score gives the learning curve and drift for free. A random split is the common default in the literature
  but leaks the future and lookalikes, and flatters every model [3, 4, 10].
- **Hold out whole Judgements, never single pairs.** A pick of 1 from 8 makes 7 "A over B" pairs; they
  stand or fall together, so they must sit on the same side of the split, and they count as one test case
  when judging how much data you have [10].
- **Mark against a fixed exam, not against the reduction being tested.** Each Instrument needs one frozen
  *evaluation reading* of its raw Judgements (what counts as the right answer). Reductions, Instruments and
  Distillation methods are then all scored against the same answers. If each reduction marked its own
  exam, changing a reduction would change the test.
- **One headline metric fits every kind of evidence: pairwise accuracy among things shown together.** Of
  every pair of Items the person saw side by side and treated differently, what share does the model put
  in the right order? This is the AUC / concordance family [7, 8]. Absolute evidence (a verdict, stars)
  and relative evidence (a pick, an order) both reduce to it. Report the kind-specific metrics beside it
  (section 3), always next to what random guessing scores.
- **How much data.** Roughly 200 independent held-out Judgements tell a model from a coin toss
  (pairwise accuracy 0.70 +/- 0.06). Telling two methods apart that differ by 5 points takes roughly 450 to
  800 test cases on the same Judgements; 10 points takes about 150. Tuning settings needs its own data on
  top, or the score you report is flattered [12].
- **Three traps peculiar to this app.** The model chooses what you see (the Mix draws bangers), so
  held-out Judgements favour models like the current one [9, 13]. Lookalike walks fill the Pool with near
  copies of past favourites, so a random split tests on things already learnt. And you are not perfectly
  consistent yourself, which puts a ceiling on any score [14, 15].

## Words used here

- **Hold-out**: Judgements kept out of model building, used only to check predictions.
- **Prequential** ("predict, then learn"): test each event with a model built from all earlier events,
  then add it [5].
- **Pairwise accuracy**: share of held-out "A over B" facts the model orders correctly; ties count half.
- **AUC**: the probability that a randomly chosen positive Item scores above a randomly chosen negative one
  [7]. Pairwise accuracy between the positive and negative groups; 0.5 is random, 1.0 perfect.
- **Concordance (C-index)**: the same idea for graded labels: the share of correctly ordered pairs among
  all pairs whose labels differ [8].
- **NDCG**: a list score that rewards putting the best Items at the top, with a discount for lower
  positions, divided by the best possible score so it runs 0 to 1 [16, 17].
- **Kendall's tau**: agreement between two orderings, -1 to 1; the tau-b variant handles ties [18].
- **Log loss**: how surprised a probabilistic model is by what you actually did; lower is better.
- **Paired comparison**: two models scored on the *same* test Judgements, so differences between
  Judgements cancel out.

## 1. Hold-out designs

**Random split.** Shuffle Judgements, hold out a share (or k folds). Cheap, uses all data, the default in
much published work. Meng et al. show the choice of split alone can reorder which recommender wins, making
results non-comparable [3]. Ji et al. show random and per-user splits leak the future: models are tested
at moments when they have already "seen" later behaviour [4]. scikit-learn's own docs warn that shuffling
ordered data gives "an inflated validation score" because test cases are "artificially similar (close in
time) to training samples" [10]. Here that bites twice: lookalike walks put near copies of a favourite in
the same weeks, and History edits rewrite an old Item's resolved value later.

**Temporal split (one cut).** Build on everything before a date, test on everything after. No future
leaks [3, 4]. At a few hundred Judgements it wastes most of the data as test or most as training, and the
one cut point is arbitrary.

**Rolling origin / prequential.** Many cuts in sequence: build on Judgements 1..t, test on t+1 (or the next
block), move t forward. scikit-learn's `TimeSeriesSplit` is the blocked version ("successive training sets
are supersets of those that come before them") [10]. Gama et al. prove that for stable data the
prequential error converges to the same value as a hold-out estimate, and argue for it with a forgetting
factor when taste drifts [5]. Vinagre et al. adapt it to recommenders, including significance tests over a
sliding window to compare two algorithms as the stream runs [6]. It suits an append-only log with a
sequence number (Invariant 4, Settled 8-10): "build to log position t" is already how derived things are
stamped. The cost is rebuilding the model many times; rebuilding every k Judgements instead of every one
trades a little freshness for speed.

**Grouped split.** Keep whole groups on one side: "the same group is not represented in both testing and
training sets" (`GroupKFold`) [10]. The natural groups here are a Judgement (all pairs from one pick), a
Batch (all verdicts on one screen) and a near-duplicate cluster (a ban's reposts). Grouping combines with
either of the above: prequential replay already keeps a Judgement whole if it steps by Judgement.

**By Instrument.** Hold out everything one Instrument recorded and build from the rest (scikit-learn's
`LeaveOneGroupOut`, whose docs give "multiple experiments" as the use case) [10]. This answers a different
question: *does evidence from Instruments A and B describe the same taste?* If verdict-grid evidence
predicts pick-1-of-n Judgements as well as pick evidence does, the two agree. It is a check on Instruments
and reductions, not the main score, and it needs a few hundred Judgements per Instrument to say anything.

## 2. What a test case is

The unit held out is one raw Judgement: the set shown, the Instrument, the responses (Settled 3). Three
consequences:

- **Pairs inside one Judgement are not independent.** A verdict-grid Batch with 2 favourites and 20
  ignores yields 40 pairs; one bad screen moves all 40. Error bars come from resampling Judgements, not
  pairs (a "cluster" bootstrap) [11]. The pair count overstates the evidence.
- **The answer is fixed at the time of the Judgement.** If a History edit later turns a like into an
  ignore, the protocol must choose: test against what was recorded then, or against the latest resolution
  (Invariant 4). The first is honest replay; the second uses information from after the test moment.
  Both are readings of an unchanged log (Settled 8), so both can be offered, but one must be the headline.
- **Only Items actually shown can be marked.** An Item in the Pool that was never shown has no label. It
  cannot fairly count as "not liked". Krichene and Rendle show that even scoring against a *random sample*
  of unshown Items flips which model wins [1, 2]. The honest comparisons are among things shown together.

## 3. Metrics for each kind of evidence

Each Instrument's frozen evaluation reading turns a raw Judgement into one or more of these answer shapes.

**Absolute, two-level ("liked or not": favourite and like vs ignore and ban).**
- AUC over the held-out Items, or within each Batch then averaged. Scale-free, so it compares methods that
  score on different scales. `sklearn.metrics.roc_auc_score` [19]. Random = 0.5.
- Sign accuracy: today's Zones split on the sign of the Score (banger is positive, dud negative), so "did
  the sign predict the verdict" is the metric closest to what the app does with the number.

**Absolute, graded (four verdicts as an order Ban < Ignore < Like < Favourite; stars 1-5).**
- Concordance: share of correctly ordered pairs among Items with different grades [8]. Generalises AUC.
- Kendall's tau-b between Score and grade, within a Batch, averaged; tau-b handles the many ties [18].
- NDCG over a Batch, with grades as gains [16, 17]. Caution: on short lists random ordering scores high.
  With one relevant Item in a list of 8, random NDCG is about 0.49; in a list of 24, about 0.33. Always
  report it against the random baseline for the same list sizes.
- Herlocker et al. found accuracy metrics on one dataset fell into about three groups that agree within
  a group and not across [20]; Gunawardana and Shani warn different metrics can favour different
  algorithms [21]. One metric per answer shape, chosen in advance, avoids picking the flattering one later.

**Relative, pairwise ("A over B").**
- Pairwise accuracy, ties counted half. Random = 0.5.
- If the model gives probabilities (Bradley-Terry style: P(A over B) from the score difference), log loss
  too. Joachims et al. found relative judgements from user behaviour reasonably accurate even where the
  same behaviour read as absolute labels was biased [22], one reason to put relative evidence first.

**Relative, pick 1 of n.**
- Top-1 hit rate: did the model's top Item equal the pick? Random = 1/n.
- Mean reciprocal rank of the picked Item. Random for n = 8 is about 0.34; for n = 4, about 0.52.
- Log loss under a Luce choice model (the probability of a pick is its share of exp(score) over the set),
  after fitting one "temperature" so scores become probabilities. `choix` fits Bradley-Terry, Plackett-Luce
  and top-1 choice models from exactly these data shapes [23].
- Pairwise accuracy of "pick over each of the others" folds it into the headline metric.

**Relative, full order (drag to order).**
- Kendall's tau between the model's order and the person's [18]; or NDCG with position as gain.

**Weak evidence (Skip, scrolled past, endless-wall passes).** Settled 7 starts these at zero weight. They
are evidence to *train* on once tuned, not answers to *test* against: an untouched Item may mean "not
looked at". Keep them out of the frozen exam; test whether giving them weight improves the score on the
strong evidence.

**Combining.** Report each answer shape separately, plus the headline pairwise accuracy pooled over all
"shown together, treated differently" pairs, weighted so each Judgement counts once (otherwise one big
Batch outweighs ten picks).

## 4. How much data each needs

Back-of-the-envelope, 95% intervals, assuming independent test Judgements.

**One model's score, how precise.**
- Pairwise accuracy near 0.70 (binomial): 100 test cases +/- 0.09; 200 +/- 0.06; 400 +/- 0.045;
  1,000 +/- 0.03.
- AUC near 0.75 (Hanley and McNeil's standard error [7]), equal positives and negatives: 25 of each
  +/- 0.14; 50 +/- 0.10; 100 +/- 0.07; 200 +/- 0.05. The scarcer class sets the precision: 30 favourites
  among 500 ignores is closer to the 25-each row than the 200-each row.
- So "is it better than random" needs about 100 to 200 independent test Judgements; a score worth quoting
  to two decimal places needs about 1,000.

**Two models, is one better.** Score both on the same test Judgements and look only at where they
disagree (McNemar's test, which Dietterich found has a low false-alarm rate and suits models trained
once [11]). With 80% power at the 5% level:
- 10-point gap, models disagree on 20% of cases: about 150 test cases.
- 5-point gap, disagree on 15%: about 450; disagree on 25%: about 800.
- 3-point gap, disagree on 10%: about 850.
- For metrics that are not right/wrong (NDCG, log loss), a paired randomisation or bootstrap test over
  Judgements: Smucker et al. found the randomisation, bootstrap and paired t-tests agree in practice [24].
- Avoid the t-test over repeated random train/test splits: Dietterich found its false-alarm rate high [11].

**Tuning costs extra.** Picking the best of many settings (radius, decay, number of prototypes) on the same
data you report on flatters the winner, by amounts comparable to the real differences between methods
(Cawley and Talbot [12]). Tune on one stretch of the log and report on a later stretch never used for
tuning.

**The ceiling.** People re-rating the same items disagree with themselves: Amatriain et al. measured RMSE
between 0.557 and 0.816 on repeat ratings [14]; Said et al. call this the "magic barrier", the best any
recommender can do on noisy ratings [15]. A model cannot score above your own consistency, so a plateau
may be you, not the model. Measuring it means re-showing decided Items, which collides with the deferred
"showing decided Wallpapers again" (map, Not yet specified).

## 5. One protocol for Instruments, reductions and Distillation methods

The parts that stay fixed: the log, the replay order, the frozen evaluation readings, the metrics, and the
test set. The part that varies is one stage of the pipeline at a time.

- **Reductions.** Hold the Instrument's evaluation reading fixed; swap the reduction used to *train*.
  Replay and score. A reduction is better if it predicts the frozen answers better. Changing the exam
  itself is a separate, versioned event, and old results are rerun against the new exam.
- **Instruments.** Two questions. *Do they agree?* By-Instrument hold-out (section 1). *Which teaches more
  per minute?* Build from equal amounts of evidence from each (equal Judgements, or better equal time,
  which needs time spent recorded with each Judgement) and score on a common test set, ideally of a third
  Instrument or a held-out period of both. Plot score against evidence used: learning curves show which
  rises faster.
- **Distillation methods.** Two numbers. *Accuracy*: the prequential score of ranking from the Distillation
  versus ranking from the full log (today's Score), same test cases, paired test (section 4). *Fidelity*:
  how closely the Distillation's order of the Pool matches the full model's order, by Kendall's tau [18];
  this needs no labels, so it can use the whole Pool. A Distillation that is cheaper with a small paired
  loss in accuracy is the trade Settled 17 anticipates.
- **Everything stamped.** Each result carries the log position, the Space, the reductions' and readings'
  versions, and the method's settings, in the spirit of Settled 10, so results stay comparable as the
  pieces change.

## 6. Threats specific to this app

- **The model picks the test.** The Mix draws from banger, dud and unknown Zones by the current Score, so
  the Items judged are the ones the current model chose. Chaney et al. call evaluating or training on data
  from people "already exposed to algorithmic recommendations" a "pernicious feedback loop", and show by
  simulation that it homogenises behaviour without improving utility [13]; Schnabel et al. correct for it by
  weighting each judged Item by the probability it was shown [9]. That correction needs the probability
  recorded when the Batch is drawn: for wallpapi, at least which Zone and Mix each tile came from. The
  Explore mix is mostly Unknowns, but Unknown is itself defined by the model (nothing decided nearby), so
  it is not a random sample of the Pool. Comparisons *within* one shown set suffer least, because both
  Items were shown.
- **Lookalikes.** Lookalike walks are seeded by favourites and likes, and bans hide near-duplicates. A
  random split puts near copies on both sides; replay in order or group near-duplicates.
- **Drift.** Old Judgements may no longer be you. Prequential scores with a fading factor or a sliding
  window show drift as a falling curve [5, 6]; the map splits drift between the Distillation method and the
  report.
- **Small classes.** Favourites are rare. AUC's precision is set by the rarer class (section 4).

## 7. Options for the evaluation-protocol ticket

**Hold-out design.**
- A. *Prequential replay, stepping by Judgement or block* (recommended reading of the sources): all data
  tested, no leakage, drift and learning curve included. Costs many rebuilds; blocks bound it.
- B. *One temporal cut* (say the last 20%): simplest; wastes data at this size; one arbitrary cut.
- C. *Grouped random k-fold*: more stable numbers early; leaks the future [3, 4]; only for questions where
  time does not matter.
- By-Instrument hold-out sits beside any of these as a diagnostic, not a replacement.

**Which answer when History has changed a verdict.** Recorded-then (honest replay) or latest-resolution
(uses later information). Either is a reading of an unchanged log.

**Headline metric.**
- A. *Within-shown-set pairwise accuracy*, each Judgement weighted once: one number for every Instrument,
  scale-free, least hurt by selection bias. Says nothing about calibration.
- B. *Log loss under a fitted choice model*: rewards confidence where earned; needs each method to output
  probabilities (one fitted temperature) and is harder to explain.
- C. *Per-Instrument metrics only*: most faithful per Instrument; no single answer to "is it better".

**What to record now so evaluation is possible later** (feeds the Evidence log and context tickets):
the shown set and order; which Zone, Mix and Slot each tile came from (the exposure probability for [9]);
time spent on the Judgement (for "teaches more per minute"); the log position and settings the shown
Batch was ranked with.

**Re-showing for a noise ceiling.** Defer with "showing decided Wallpapers again", or reserve a small
share of Slots for re-shows once the protocol exists. Without it, a plateau cannot be told apart from the
ceiling.

## Sources

1. Krichene, W. and Rendle, S. "On Sampled Metrics for Item Recommendation." KDD 2020 (best paper).
   https://research.google/pubs/on-sampled-metrics-for-item-recommendation/
2. Rendle, S. "Evaluation Metrics for Item Recommendation under Sampling." arXiv:1912.02263, 2019.
   https://arxiv.org/abs/1912.02263
3. Meng, Z., McCreadie, R., Macdonald, C. and Ounis, I. "Exploring Data Splitting Strategies for the
   Evaluation of Recommendation Models." RecSys 2020. https://arxiv.org/abs/2007.13237
4. Ji, Y., Sun, A., Zhang, J. and Li, C. "A Critical Study on Data Leakage in Recommender System Offline
   Evaluation." arXiv:2010.11060 (v4, 2022), ACM TOIS. https://arxiv.org/abs/2010.11060
5. Gama, J., Sebastiao, R. and Rodrigues, P. P. "On evaluating stream learning algorithms." Machine
   Learning 90, 317-346, 2013. https://doi.org/10.1007/s10994-012-5320-9
6. Vinagre, J., Jorge, A. M. and Gama, J. "Evaluation of recommender systems in streaming environments."
   RecSys REDD workshop 2014. https://arxiv.org/abs/1504.08175
7. Hanley, J. A. and McNeil, B. J. "The meaning and use of the area under a receiver operating
   characteristic (ROC) curve." Radiology 143(1), 1982. https://doi.org/10.1148/radiology.143.1.7063747
8. Harrell, F. E. et al. "Evaluating the yield of medical tests." JAMA 247(18), 2543-2546, 1982 (the
   concordance index). https://scholars.duke.edu/publication/694592
9. Schnabel, T., Swaminathan, A., Singh, A., Chandak, N. and Joachims, T. "Recommendations as Treatments:
   Debiasing Learning and Evaluation." ICML 2016, PMLR 48:1670-1679.
   https://proceedings.mlr.press/v48/schnabel16.html
10. scikit-learn User Guide, "Cross-validation: evaluating estimator performance" (`GroupKFold`,
    `LeaveOneGroupOut`, `TimeSeriesSplit`, the shuffling caveat).
    https://scikit-learn.org/stable/modules/cross_validation.html
11. Dietterich, T. G. "Approximate Statistical Tests for Comparing Supervised Classification Learning
    Algorithms." Neural Computation 10(7), 1895-1923, 1998.
    https://web.engr.oregonstate.edu/~tgd/publications/dietterich-approximate-statistical-tests-nc1998.pdf
12. Cawley, G. C. and Talbot, N. L. C. "On Over-fitting in Model Selection and Subsequent Selection Bias in
    Performance Evaluation." JMLR 11, 2079-2107, 2010. https://www.jmlr.org/papers/v11/cawley10a.html
13. Chaney, A. J. B., Stewart, B. M. and Engelhardt, B. E. "How Algorithmic Confounding in Recommendation
    Systems Increases Homogeneity and Decreases Utility." RecSys 2018. https://arxiv.org/abs/1710.11214
14. Amatriain, X., Pujol, J. M. and Oliver, N. "I Like It... I Like It Not: Evaluating User Ratings Noise
    in Recommender Systems." UMAP 2009, LNCS 5535, 247-258. https://doi.org/10.1007/978-3-642-02247-0_24
15. Said, A., Jain, B. J., Narr, S. and Plumbaum, T. "Users and Noise: The Magic Barrier of Recommender
    Systems." UMAP 2012, LNCS 7379, 237-248. https://doi.org/10.1007/978-3-642-31454-4_20
16. Jarvelin, K. and Kekalainen, J. "Cumulated Gain-based Evaluation of IR Techniques." ACM TOIS 20(4),
    422-446, 2002. https://doi.org/10.1145/582415.582418
17. scikit-learn, `sklearn.metrics.ndcg_score`.
    https://scikit-learn.org/stable/modules/generated/sklearn.metrics.ndcg_score.html
18. SciPy, `scipy.stats.kendalltau` (tau-b default, ties; see also `weightedtau`).
    https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.kendalltau.html
19. scikit-learn, `sklearn.metrics.roc_auc_score`.
    https://scikit-learn.org/stable/modules/generated/sklearn.metrics.roc_auc_score.html
20. Herlocker, J. L., Konstan, J. A., Terveen, L. G. and Riedl, J. T. "Evaluating Collaborative Filtering
    Recommender Systems." ACM TOIS 22(1), 5-53, 2004. https://doi.org/10.1145/963770.963772
21. Gunawardana, A. and Shani, G. "A Survey of Accuracy Evaluation Metrics of Recommendation Tasks." JMLR
    10, 2935-2962, 2009. https://jmlr.org/papers/v10/gunawardana09a.html
22. Joachims, T., Granka, L., Pan, B., Hembrooke, H. and Gay, G. "Accurately Interpreting Clickthrough
    Data as Implicit Feedback." SIGIR 2005, 154-161. https://www.sigir.org/wp-content/uploads/2017/06/p003.pdf
23. `choix`: inference for Luce-choice models (Bradley-Terry, Plackett-Luce, top-1 choices).
    https://choix.lum.li/en/latest/
24. Smucker, M. D., Allan, J. and Carterette, B. "A Comparison of Statistical Significance Tests for
    Information Retrieval Evaluation." CIKM 2007, 623-632. https://doi.org/10.1145/1321440.1321528
