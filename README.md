# wallpapi

A personal tool for finding wallpapers you actually like.

See `CONTEXT.md` for the domain vocabulary and `AGENTS.md` for the stack and invariants.

## Running

```
uv run python -m wallpapi
```

Binds to `127.0.0.1:8000` and starts the background **Pool** refill. On first boot the similarity model
(about 85 MB) is downloaded into `~/.wallpapi/models/`; until it arrives the metadata baseline stands in.

Environment variables:

| Variable | Effect |
| --- | --- |
| `WALLPAPI_PORT` | Port to listen on (default `8000`) |
| `WALLPAPI_HOME` | Data folder for the **Decision log**, **Thumbnail cache** and models (default `~/.wallpapi`) |
| `WALLPAPI_SIMILARITY` | **Similarity provider**: `embedding` (default), `metadata` or `tags` |
| `WALLPAPI_SEED` | Pin the random source for a reproducible session |

To run under uvicorn directly, use the factory. Never pass `--workers`: more than one process means several
refill threads and several writers against one SQLite file.

```
uv run uvicorn --factory wallpapi.main:build_app --host 127.0.0.1 --port 8000
```
