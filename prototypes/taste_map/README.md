# Taste map: PROTOTYPE, throwaway

Answers one question for wallpapi-5tu: how do the real **Verdicts** group into a **Distillation**'s
prototypes (method settled in wallpapi-wa5.10), and what merge distance looks right by eye? It is not
wallpapi, shares none of its UI rules, and lives on the `prototype/taste-map` branch only.

- `snapshot.py` copies `wallpapi.db` and `embeddings.db` from the live install (default
  `/mnt/c/Users/Cameron/.wallpapi`, or `TASTE_MAP_SOURCE`) into `snapshot/` (gitignored), opening each
  source `mode=ro` and using SQLite's backup API. The live files are never written.
- `app.py` groups the embedded decided **Wallpapers** at every merge distance from 0 to 0.20 (step 0.005),
  lays each grouping out from a 2D PCA of the embeddings, and serves the page, one JSON and the thumbnails
  (read-only from the live `thumbnails/`, or `TASTE_MAP_THUMBNAILS`; an evicted one falls back to
  Wallhaven's). Distance is wallpapi's, `1 - (1 + cos) / 2`. The build takes ~20 s and is cached in
  `snapshot/map.json` until the snapshot or `app.py` changes.

Run from the repository root:

    uv run python prototypes/taste_map/snapshot.py
    uv run uvicorn prototypes.taste_map.app:app --host 0.0.0.0 --port 8765

It binds `0.0.0.0` on purpose so a phone on the home network can open it; wallpapi itself stays on
`127.0.0.1`. Under WSL2's NAT networking, Windows needs a port proxy and a firewall rule as well.
