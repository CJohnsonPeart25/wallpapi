# Image embedding model and runtime for wallpapi (researched 2026-11-22)

## Recommendation

**Keep quantized CLIP ViT-B/32 on onnxruntime for now. Evaluate SigLIP 2 B/32-256 as the one candidate.** No
small model is clearly better for wallpaper taste, judged per byte and per CPU-millisecond. SigLIP 2 B/32-256
is the only contender worth a trial:

- it is Apache-2.0 licensed, with a ready int8 image-tower ONNX of about the same size (96 MB against 89 MB);
- it costs about 1.3-1.4x the CPU;
- it is clearly better on public benchmarks.

Those benchmarks measure semantic retrieval, though, not aesthetic taste. Only a replay against the real
**Evidence log** can show whether it ranks better.

**The `evaluate` command cannot compare models today.** It only accepts `--radius`, `--decay` and `--decided`
(`src/wallpapi/main.py:137-151`). A trial needs new work:

1. Embed the **Pool** and the decided **Wallpapers** into a candidate **Space**, offline.
2. Replay the log against that **Space**.
3. Re-measure the radius and `NEAR_DUPLICATE_SIMILARITY`, because both are calibrated to CLIP's cosine spread
   (ADR 0013, ADR 0023, `taste/clip.py`).

That work is out of my read-only scope. It is worth filing as a ticket.

**Do not switch to DINOv2 or DINOv3.** Their training makes them insensitive to colour and style, the axes
wallpapers vary on most. **MobileCLIP and MobileCLIP2 are ruled out by their licence.**

## Comparison

All CPU timings below are my own measurements. Setup:

- onnxruntime 1.32.0 on Python 3.14;
- an AMD Ryzen 7 9700X desktop (Zen 5, AVX-512) under WSL2, which is faster than an ordinary laptop, so expect
  roughly 2-3x these numbers there;
- random input in batches of 16, giving ms per image.

| Model (int8 image tower) | Licence | Download | 1 thread | 4 threads | Style/visual evidence | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **CLIP ViT-B/32** (current, Xenova) | MIT | 89 MB | 20 ms | 9 ms | CLIP beats DINO on style retrieval | Keep |
| **SigLIP 2 B/32-256** (onnx-community) | Apache-2.0 | 96 MB | 29 ms | 11 ms | SigLIP ≈ CLIP on style; better retrieval | **Evaluate** |
| SigLIP 2 B/16-224 | Apache-2.0 | 95 MB | 103 ms | 41 ms | Stronger still | Too slow for the gain |
| PE-Core B16-224 (Meta) | Apache-2.0 | 95 MB | 90 ms | 30 ms | No style data | Too slow; export fixes batch at 2 |
| DINOv2-small | Apache-2.0 | 24 MB | 37 ms | 13 ms | Worst on style (content-dominated) | No |
| DINOv3 ViT-S/16 | DINOv3 Licence (custom) | 22 MB | 30 ms | 11 ms | Same SSL colour invariance | No |
| MobileCLIP / MobileCLIP2 S0 | Apple AMLR (research only) | 12-46 MB | not run | not run | n/a | Licence rules it out |
| TinyCLIP 8M/16 | MIT | 24 MB (both towers) | not run | not run | Much weaker zero-shot | No |
| Nomic Embed Vision v1.5 | Apache-2.0 | 97 MB | not run (ViT-B/16 class, ≈ SigLIP 2 B/16 cost) | | None | No |
| Jina CLIP v2, MetaCLIP (2) | CC-BY-NC | large | | | | Licence rules them out |
| EmbeddingGemma 2 (Oct 2026) | Apache-2.0 | vision encoder alone 109 MB at q4, 671 MB fp32 | not run | | No public image-similarity data | Too heavy; image path unverified |

## Key evidence

**Style similarity: CLIP and SigLIP beat DINO.**

- CSD, "Measuring Style Similarity in Diffusion Models" (arXiv 2404.01292, Apr 2024), WikiArt mAP@1: CLIP
  ViT-B/16 scores 52.2 and DINO ViT-B/16 scores 44.0. The authors note that self-supervised training makes
  models invariant to style, through photometric augmentation (colour jitter, greyscale).
  https://arxiv.org/html/2404.01292v1
- StyleDecoupler (arXiv 2601.17697, 25 Jan 2026), WikiArt mAP@1 at ViT-L: DINOv2 40.0, CLIP 58.4, SigLIP 58.5.
  The authors call DINOv2's features "content-dominated". https://arxiv.org/html/2601.17697v1
- Wallpaper taste leans heavily on colour and mood, so this is the decisive point against DINO. Evidence is
  thin on *aesthetic* similarity as such. No benchmark matches "wallpaper taste".

**SigLIP 2** (arXiv 2502.14786, 20 Feb 2025), on ImageNet zero-shot and COCO retrieval:

- B/32-256: ImageNet 74.0, COCO text-to-image 47.2 / image-to-text 63.7.
- For comparison, OpenCLIP B/32-256 scores 72.8 / 39.9 / 57.9, and the original OpenAI CLIP B/32 is commonly
  cited at 63.3 on ImageNet.
- These are semantic benchmarks.

https://arxiv.org/html/2502.14786. The model card for `google/siglip2-base-patch32-256` lists Apache-2.0
(created 2025-02-17).

**Licences.**

- MobileCLIP2 and now MobileCLIP v1 on Hugging Face are under the Apple ML Research Model licence: "Research
  Purposes ... does not include any commercial exploitation, product development".
  https://huggingface.co/apple/MobileCLIP2-B (licence read 2026-11-22). `apple/MobileCLIP-S0` is also tagged
  `apple-amlr`.
- The DINOv3 Licence allows redistribution, but requires a copy of the licence, a "Built with DINOv3" notice
  and a ban on military uses. https://ai.meta.com/resources/models-and-libraries/dinov3-license
- EmbeddingGemma 2 has a 170M-parameter vision encoder, Apache-2.0, announced 2026-10-06.
  https://blog.google/innovation-and-ai/technology/developers-tools/embeddinggemma-2/

**Runtime.**

- onnxruntime has shipped Python 3.14 wheels since 1.24.1. 1.32.0 (PyPI, 2026-11-13) has cp314 wheels for
  `win_amd64`, `win_arm64`, `macosx_14_0_arm64` and `manylinux_2_28` x86_64/aarch64. The repo pins 1.30.0
  (2026-09-10), which has the same platform set. https://pypi.org/pypi/onnxruntime/json
- OpenVINO 2026.4.1 has cp314 wheels for Windows x64, macOS arm64 and Linux, but none for Windows on ARM. It
  gains nothing on Apple or AMD CPUs, and at about 10 ms per image the CPU is not the bottleneck.
- The CoreML execution provider ships in the official macOS wheel.
  https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html. It is unlikely to help an
  int8 model built from dynamic-quantisation ops; I have not verified this.
- Pure numpy is possible for a ViT-B/32 but means fp32 weights (about 350 MB) and a hand-written forward pass.
  It is not worth it.
- At my measured speed, re-embedding a whole switch costs about 20 s to a minute: roughly 2,000 thumbnails,
  which is the 1,322 **Pool** members plus 692 decided in ADR 0023.

**Journeys.** The standard method is to treat the ordering as an open travelling-salesman path on `1 -
cosine`:

- Start with greedy nearest-neighbour from a chosen start, then improve it with 2-opt.
- For a few hundred **Favourites** this is a few milliseconds in numpy, with no new dependency.
- Spectral seriation (the order of the Fiedler vector of the similarity graph) or a 1-D UMAP gives a global
  order but jumps locally. UMAP also adds a dependency.
- Exact solvers (LKH via `elkai`, OR-Tools) are overkill at this size.

This is established technique. I did not check primary sources for it.

**Day and night.**

- Brightness needs no model: take the mean relative luminance of the thumbnail (sRGB linearised, Rec. 709
  weights) or the median CIELAB L*, plus the share of dark pixels.
- Semantic "night-ness" through zero-shot prompts needs the text tower. Instead, compute the prompt embeddings
  once offline per **Space** and ship them as constants (512 floats each). Alternatively, fit a linear probe
  on the user's own day and night labels.

## Risks and unknowns

- **No evidence on wallpaper taste.** Every model comparison above is semantic or art-style retrieval. Only
  the replay on the real log can decide.
- **Preprocessing differs.** SigLIP 2 squashes the image to 256x256 with mean and std of 0.5. CLIP resizes the
  short side, centre-crops and uses CLIP's mean and std. The squash keeps a wide wallpaper's whole
  composition, which may help or hurt. `taste/clip.py` would need a preprocessing spec per model.
- **Calibration.** The radius (0.10), the near-duplicate threshold (0.985) and the cosine-to-[0, 1] mapping
  were all tuned on CLIP's distribution. SigLIP's cosines spread differently. A switch changes the **Space**,
  re-embeds everything and needs a new migration for the radius.
- **int8 accuracy.** I did not measure how far int8 drifts from fp32 for SigLIP 2. The current CLIP int8 is
  proven in the app.
- **Latency.** Measured on one fast desktop with synthetic input. Laptop numbers are an estimate.
- **Platform gaps.** onnxruntime has no Intel-Mac wheels and nothing for macOS older than 14. That is a gap in
  the packaged app whichever model is chosen.
