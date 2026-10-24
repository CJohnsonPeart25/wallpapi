# A CLIP text tower matching the pinned vision model

Research for `wallpapi-wa5.7`, feeding the Lenses and Blends ticket. Settled 15 (words as Lenses, steering
only, never evidence) is taken as given. Researched 2026-10-24.

## Answer

- **Yes, it is there.** `Xenova/clip-vit-base-patch32` at the pinned revision `d15189d…`, the repository
  wallpapi already downloads from, holds the text half of the same CLIP model. Its quantized text file was
  uploaded in the same commit as the vision file wallpapi uses. It takes token numbers in and gives back a
  512-number vector in the same space as the image vectors wallpapi already stores. Tested on 1,001 of your
  own thumbnails, it works: the words "an anime illustration" pick out Wallhaven's anime category far better
  than chance.
- **Size: 61.5 MiB quantized, 121 MiB half-precision, 242 MiB full.** The quantized one is cheapest but
  visibly worse at mood words: with it, the "light and airy" ranking bore no relation to brightness; with
  the larger files it did, and looked right. The half-precision file scored the same as the full one on
  every test, at half the size.
- **Licence: the same position as the image file you already ship.** The Hugging Face repo declares no
  licence. OpenAI's CLIP repository is MIT. OpenAI's model card calls any deployed use "out of scope",
  which is a statement of intent, not a licence term. The text file adds no new exposure.
- **Tokenizer: no heavy dependency is needed.** The text model needs CLIP's own word-splitting (byte-pair
  encoding, lower-cased, at most 77 tokens). About 60 lines of standard-library Python, reading two files
  from the same revision, matched Hugging Face's tokenizer on 13 of 14 test phrases. The odd one out was an
  HTML entity, where the 60 lines follow OpenAI's original. The `regex` package (one 284 KB wheel, no
  dependencies) would make the rare-script cases exact. `tokenizers` needs `huggingface-hub` and its tail;
  `onnxruntime-extensions` has no Windows or Python 3.14 wheel.
- **How well words steer: well for things, middling for moods.** Concrete scenes ("a cyberpunk city at
  night", "a cosy autumn scene", "minimalist") came back looking right. Mood phrases half-work: "dark and
  moody" found dark images, but also portraits, and CLIP ignores "not". Pairing a phrase with its opposite
  ("dark and moody" against "bright and cheerful") was clearly stronger than the phrase alone. That matches
  the published work on using CLIP to judge how images look and feel.
- **One thing to plan for:** a text-to-image match scores in a narrow band (cosine about 0.16 to 0.26),
  far below image-to-image (about 0.36 to 0.71). A Lens score cannot be added raw to today's similarities;
  it needs rescaling first.

An aside outside this question, for the lead: the quantized *image* model gives a slightly different vector
for the same image depending on what else is in its batch (see the end).

## What is in the pinned repository

The revision is `d15189d7028b43f1d3e65039190477f6af591c2a` (2025-07-08, "Add/update the quantized ONNX
model files and README.md for Transformers.js v3"). Its `onnx/` folder holds three families: `model*` (both
towers in one graph), `vision_model*` and `text_model*` [1]. The text files:

| File | Bytes | MiB | sha256 (first 12) | Last changed |
| --- | ---: | ---: | --- | --- |
| `text_model.onnx` (float32) | 254,058,553 | 242.3 | `3f6571f5bad1` | 2023-07-29 |
| `text_model_fp16.onnx` | 127,339,794 | 121.4 | `df587ffbf248` | 2024-03-16 |
| `text_model_quantized.onnx` | 64,504,507 | 61.5 | `73baab855d40` | 2023-07-29 |
| `text_model_int8.onnx` | 64,070,791 | 61.1 | `18845f2ccc35` | |
| `text_model_uint8.onnx` | 64,070,835 | 61.1 | `cbe64fd9eaa6` | |
| `text_model_q4f16.onnx` | 72,531,963 | 69.2 | `7640460b1389` | |
| `text_model_q4.onnx` | 125,742,108 | 119.9 | `e4ccd15d806b` | |
| `text_model_bnb4.onnx` | 123,367,012 | 117.7 | `b7c2b531428f` | |

Full sha256s: quantized is
`73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a`; fp16 is
`df587ffbf248bf20d44fa6e16adc5ebc27ead691860e5333dbdaab5fd6bf3f6e`; float32 is
`3f6571f5bad13a97c469c1622e1cfc4d9aef78b79fdbfcff804ca357bfada8cc`. Downloads at the pinned revision were
checked against these [1]. The 4-bit variants were not tried.

**Same model, same space.** The evidence, strongest first:

- `vision_model_quantized.onnx` (wallpapi's file, sha256 `583fd111…` as pinned in `similarity.py`),
  `text_model_quantized.onnx` and `text_model.onnx` were all last changed by one commit, "Upload 4 files",
  2023-07-29 [1].
- The repo's one `config.json` describes a single `CLIPModel` with `projection_dim: 512`, exported from
  `openai/clip-vit-base-patch32` (the README's `base_model`) [1].
- Opened in onnxruntime, the text file takes `input_ids` (int64, any batch, any length) and returns
  `text_embeds` (512 floats); the vision file returns `image_embeds` (512 floats). Both are the projected,
  shared-space outputs, not the towers' inner states.
- Both quantized files were made by the same tool (`onnx.quantize 0.1.0`) with the same scheme: dynamic
  8-bit, `DynamicQuantizeLinear` plus `MatMulInteger` (73 of each tower's matrix products).
- It works end to end on wallpapi's stored vectors: the phrase "an anime illustration" ranks the 252
  Wallhaven-category anime thumbnails above the other 749 with an AUC of 0.91 (quantized text) to 0.96
  (full text); a random direction scores 0.43. (AUC: the chance a random anime thumbnail outranks a random
  other one; 0.5 is a coin toss, 1.0 perfect.)

## Licence

- `Xenova/clip-vit-base-patch32` declares no licence: its card metadata has only `base_model` and
  `library_name`, and the README says only that it is the OpenAI model "with ONNX weights to be compatible
  with Transformers.js" [1].
- The upstream Hugging Face card, `openai/clip-vit-base-patch32`, carries no licence tag either [2].
- OpenAI's CLIP GitHub repository is MIT-licensed, "Copyright (c) 2021 OpenAI" [3]. The licence text names
  "this software and associated documentation files"; it does not mention weights explicitly.
- OpenAI's model card (same text on GitHub and Hugging Face): "**Any** deployed use case of the model -
  whether commercial or not - is currently out of scope", and surveillance and facial recognition are
  "always out-of-scope" [2][4]. This is guidance on intended use, not a licence condition.

The image tower wallpapi already runs sits in exactly this position (ADR 0013), so adding the text tower from
the same repository changes nothing about it. If a future platform ever distributes it, this is worth a
proper look then.

## The tokenizer

**What the model needs.** CLIP's text side reads a "lower-cased byte pair encoding (BPE) representation of
the text with a 49,152 vocab size", capped at 76 tokens plus start and end markers, and the vector is read
off at the end marker [5]. The repo's `tokenizer_config.json` names `CLIPTokenizer`, `do_lower_case`,
`model_max_length: 77`, start `<|startoftext|>` and end and pad `<|endoftext|>` [1]. In token numbers: start
49406, end 49407.

The reference implementation is OpenAI's `clip/simple_tokenizer.py`, 132 lines [6]. It does three things:
tidies the text (`ftfy.fix_text`, HTML unescape twice, collapse whitespace, lower-case); splits it with one
regular expression that uses Unicode letter and number classes (`\p{L}`, `\p{N}`), which the standard
library's `re` lacks; and runs byte-level BPE over each piece. It imports `ftfy` and `regex` [6].

Practical facts measured here:

- The ONNX text model has no attention-mask input. Padding to 77 is unnecessary: unpadded and padded input
  gave identical vectors on the float models (cosine 1.000000) and near-identical on the quantized one
  (0.9993, a side effect of dynamic quantization).
- Encoding one phrase takes 1.3 ms (quantized), 3.7 ms (float32) or 4.7 ms (fp16) on this machine, after a
  0.2 to 0.4 s session load. Cost is not a factor.

**Options, lightest first:**

| Option | New dependency | Fidelity | Notes |
| --- | --- | --- | --- |
| A. Own ~60-line BPE, stdlib `re` | none | 13/14 probes same as HF | Reads `vocab.json` and `merges.txt` |
| B. As A, with `regex` | `regex` | exact to OpenAI's splitting | 284 KB wheel, no deps, has cp314 |
| C. HF `tokenizers` | `tokenizers` + `huggingface-hub` | HF's own | Loads `tokenizer.json` directly |
| D. `onnxruntime-extensions` | itself | n/a | Not installable here |

- **A.** Written and tested for this research (it lives outside the repo). The two files are 862,328 and
  524,619 bytes at the same revision and would be pinned and checksummed like the model. The standard
  library stands in for `\p{L}` with `[^\W\d_]`, which is identical for English and accented Latin text and
  may split some rarer scripts differently. Probes that matched: plain phrases, punctuation, apostrophes,
  hyphens, digits, accents (`café naïve façade`), Japanese, an emoji, extra whitespace. The one mismatch,
  `&amp; html &lt;3`, is OpenAI's HTML unescaping, which Hugging Face's `tokenizer.json` does not do. `ftfy`
  is skipped: it only repairs mis-decoded text, which a typed phrase does not have.
- **B.** `regex` 2026.9.29, Apache-2.0 and CNRI-Python, no dependencies, Windows wheel for CPython 3.14
  [7]. Removes A's only approximation.
- **C.** `tokenizers` 0.23.3 ships a 2.9 MB abi3 wheel, but requires `huggingface-hub`, which in turn
  requires click, filelock, fsspec, hf-xet, httpx2, packaging, pyyaml, tqdm and typing-extensions [7]. The
  stack is locked; this is a lot of tail for a 77-token splitter.
- **D.** `onnxruntime-extensions` 0.15.2 (the latest) ships wheels for macOS and Linux, CPython 3.10 to 3.13
  only: none for Windows and none for 3.14 [7]. Ruled out.
- Ruled out without testing: OpenAI's `clip` package and `open_clip` both need PyTorch.

## How well words steer a ranking

### What the papers say

- **It is the model's designed use.** CLIP was trained so an image and a caption describing it land close
  together; zero-shot classification is exactly "which phrase is this image nearest" [5].
- **Wording matters, and averaging wordings helps a little.** On ImageNet, averaging the text vectors of 80
  prompt templates added 3.5 points over one default prompt; prompt engineering and ensembling together
  "almost 5%" [5].
- **A phrase alone is a weak judge of look and feel; a pair of opposites is much better.** CLIP-IQA found
  that scoring an image against a single prompt ("Good photo") is "not viable" for perception, and pairs
  each prompt with its antonym ("Good photo." and "Bad photo.", "Bright photo." and "Dark photo."), so the
  score is relative [8]. It reports CLIP judging abstract qualities this way (happy and sad, scary and
  peaceful, complex and simple, aesthetic). Template wording swung its rank agreement with human quality
  scores from 0.12 to 0.70, and ViT-B/32 (wallpapi's model) did worse than CLIP's ResNet-50 there (0.39 to
  0.63 against 0.70, depending on the setting) [8].
- **Text and image vectors live apart.** The "modality gap": images and text are "embedded at arm's length"
  in CLIP's shared space, an effect of how such models are initialised and trained [9]. In practice every
  text-to-image cosine is small and bunched, whatever the match.
- **CLIP reads its prompts as a bag of words.** It has "a severe lack of order sensitivity" and "poor
  relational understanding" [10]; "a red car by a blue house" and "a blue car by a red house" score alike.
- **It does not understand "not".** Vision-language models "struggle significantly with negation, often
  performing at chance level" [11].
- **It reads text in images.** CLIP is vulnerable to "typographic attacks" where text in the image drives
  the match [12].

### What it did on your thumbnails

Setup: the 1,001 thumbnails in `~/.wallpapi/thumbnails`, embedded by wallpapi's quantized image file (the
stored vectors, or the same thumbnails re-embedded in batches of 16 where precisions are compared), scored
against text vectors from each text file. Darkness was checked against each thumbnail's mean brightness (a
proxy: it measures "dark", not "moody"). "rho" is Spearman's rank agreement: +1 the same order, 0 unrelated,
-1 reversed. The eyeball checks are mine, on contact sheets of each Lens's top 18.

| Lens | Brightness rho, quantized text | Same, full text | Eyeball, top 18 |
| --- | ---: | ---: | --- |
| "a cyberpunk city at night" | | | all on theme: neon cities, synthwave |
| "a cosy autumn scene" | | | autumn forests and leaves, warm rooms |
| "a minimalist wallpaper" | | | flat vector art, gradients, logos |
| "dark and moody" | -0.38 | -0.40 | mostly dark; also lit portraits |
| "light and airy" | -0.03 | +0.29 | quantized: glowing colourful art |
| "dark and moody" vs "bright and cheerful" | -0.45 | -0.55 | very dark; grim game and horror art |
| "light and airy" vs "dark and moody" | +0.35 | +0.56 | full text: soft sunlit photos |

What that says:

- **Concrete subjects work out of the box.** These were convincing even with the quantized text tower.
- **Mood words are where the text tower's precision shows.** The full and fp16 text files agreed exactly
  with each other and beat the quantized one on every measure (table below). "Light and airy" went from no
  relation to brightness to a clear one, and its top 18 changed from neon art to soft sunlit photography.
- **Opposites beat a lone phrase.** Pairing raised the darkness agreement in every combination, as CLIP-IQA
  predicts [8]. A lone mood phrase also carries a generic "atmospheric picture" pull: alone, "dark and
  moody" and "light and airy" ranked the pool in fairly similar order (rho +0.41 quantized, +0.19 full).
- **Templates did not help here.** Averaging five wordings of "dark and moody" ranked almost identically to
  the bare phrase (rho +0.94) with no better darkness agreement.
- **"Not" is ignored.** "dark" and "not dark" ranked the pool in nearly the same order (rho +0.87).
- **Words in the picture count.** The darkest-mood list included posters whose titles carry the meaning
  ("THE CROW", "ULTRAKILL").
- **Taking the Pool's average out first changes nothing** for one Lens's ranking (darkness rho -0.38 to
  -0.39): it shifts every score by about the same amount.

Precision of each tower, with the full-text-and-full-image ranking as the reference:

| Image x text | Anime AUC | Dark pair rho | Light pair rho | Top-50 overlap |
| --- | ---: | ---: | ---: | ---: |
| quantized x quantized | 0.914 | -0.45 | +0.35 | 29.5/50 |
| quantized x fp16 | 0.958 | -0.55 | +0.56 | 35.6/50 |
| quantized x float32 | 0.958 | -0.55 | +0.56 | 35.6/50 |
| float32 x quantized | 0.921 | -0.49 | +0.45 | 32.9/50 |
| float32 x float32 | 0.965 | -0.60 | +0.62 | 50/50 |

"Quantized image" is the vision file wallpapi runs today. The text tower's precision matters more than the
image tower's: upgrading text alone recovers most of the gap, so a Lens does not require re-embedding the
Pool.

**Score scale.** On these thumbnails the raw cosine for a lone phrase sat between about 0.16 and 0.26 (5th
to 95th percentile), and an opposites pair between -0.03 and +0.03, against 0.36 to 0.71 for image to image.
This is the modality gap [9] and it is why ADR 0013's mapped `[0, 1]` similarities and a Lens score cannot be
mixed as they come.

Caveats: one person's 1,001 thumbnails, already shaped by their own Verdicts; one person's eyeball; brightness
is the only objective check of a mood and covers only its "dark" half; no other model was compared.

## Options for the Lenses ticket

Decisions belong to the grilling ticket; these are the choices this research exposes.

1. **Which text file.**
   - fp16, 121 MiB: matched float32 on every measure here at half its size. Recommended by the evidence.
   - Quantized, 61.5 MiB: fine for concrete subjects, weak on mood words, which are the headline use.
   - float32, 242 MiB: no gain over fp16 here.
   Either way a second pinned, checksummed download, fetched in the background like the image model; it
   could wait until the first Lens is asked for, rather than coming at first boot.
2. **What a Lens is made of.** A lone phrase (simplest, weakest for moods); a phrase and its opposite (a
   second box, or an opposite the app supplies); or several phrases averaged (cheap; no gain measured here).
   The phrase-and-opposite form is the one with published and measured support for moods.
3. **How a Lens score meets today's numbers.** Raw text cosines live in their own narrow band. Options:
   rescale per ranking (each Pool member's rank, or its distance from the Pool's mean in standard
   deviations), or use the Lens only to reorder within what Distillation already ranks. Any of these keeps
   Invariant 2: one matrix product, Pool x Lens vectors, computed per ranking, never stored.
4. **What the page says about limits.** "Not" does nothing, word order barely matters, and text inside an
   image can dominate. Whether the app warns, or strips "not", is a product call.
5. **Tokenizer.** Option A (no new dependency) or B (`regex`, exact); C and D as above.

## Aside, outside this question: batches change image vectors

The quantized image tower computes its 8-bit scaling "on the fly" from each input tensor [13], and ONNX's
`DynamicQuantizeLinear` takes that range over the whole tensor [14], so over the whole batch. Measured on the
same 1,001 thumbnails, one image embedded alone and embedded among 16 differs: cosine median 0.985, minimum
0.903. Re-embedding five stored thumbnails one at a time gave 0.974 to 0.990 against the stored vectors.
In wallpapi's mapped scale, `(1 + cosine) / 2`, an identical image embedded in two different batches can
land between 0.95 and 0.995, either side of `NEAR_DUPLICATE_SIMILARITY = 0.985` (ADR 0021). The quantized and
float32 image towers also differ more than expected for the same image (median cosine 0.93, minimum 0.74).
Not investigated further; reported to the lead for triage.

## Sources

1. Hugging Face, `Xenova/clip-vit-base-patch32` at `d15189d7028b43f1d3e65039190477f6af591c2a`: file tree,
   commits, `README.md`, `config.json`, `tokenizer_config.json`.
   <https://huggingface.co/Xenova/clip-vit-base-patch32/tree/d15189d7028b43f1d3e65039190477f6af591c2a>
2. Hugging Face, `openai/clip-vit-base-patch32` model card.
   <https://huggingface.co/openai/clip-vit-base-patch32>
3. OpenAI CLIP, `LICENSE` (MIT). <https://github.com/openai/CLIP/blob/main/LICENSE>
4. OpenAI CLIP, `model-card.md`. <https://github.com/openai/CLIP/blob/main/model-card.md>
5. Radford et al., "Learning Transferable Visual Models From Natural Language Supervision", 2021, sections 2.4
   and 3.1.4. <https://arxiv.org/abs/2103.00020>
6. OpenAI CLIP, `clip/simple_tokenizer.py`.
   <https://github.com/openai/CLIP/blob/main/clip/simple_tokenizer.py>
7. PyPI JSON metadata for `regex` 2026.9.29, `tokenizers` 0.23.3, `huggingface-hub` 2.3.0,
   `onnxruntime-extensions` 0.15.2. <https://pypi.org/pypi/regex/json> (and likewise for each)
8. Wang, Chan and Loy, "Exploring CLIP for Assessing the Look and Feel of Images" (CLIP-IQA), AAAI 2023,
   sections 2.1, 2.3 and table 2. <https://arxiv.org/abs/2207.12396>
9. Liang et al., "Mind the Gap: Understanding the Modality Gap in Multi-modal Contrastive Representation
   Learning", NeurIPS 2022. <https://arxiv.org/abs/2203.02053>
10. Yuksekgonul et al., "When and why vision-language models behave like bags-of-words, and what to do about
    it?", ICLR 2023. <https://arxiv.org/abs/2210.01936>
11. Alhamoud et al., "Vision-Language Models Do Not Understand Negation" (NegBench), 2025.
    <https://arxiv.org/abs/2501.09425>
12. Goh et al., "Multimodal Neurons in Artificial Neural Networks", Distill, 2021.
    <https://distill.pub/2021/multimodal-neurons/>
13. ONNX Runtime, "Quantize ONNX models", dynamic quantization.
    <https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html>
14. ONNX operator specification, `DynamicQuantizeLinear`.
    <https://github.com/onnx/onnx/blob/main/docs/Operators.md#DynamicQuantizeLinear>
