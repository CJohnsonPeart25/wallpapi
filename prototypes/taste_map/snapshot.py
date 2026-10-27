"""PROTOTYPE, throwaway. Copy the live install's two databases into `snapshot/` beside this file.

Each source is opened read-only (`mode=ro` URI) and copied with SQLite's backup API, so the live app's files
are never written and a write in progress is seen whole or not at all. Run it again for fresh data.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SOURCE = Path(os.environ.get("TASTE_MAP_SOURCE", "/mnt/c/Users/Cameron/.wallpapi"))
SNAPSHOT = Path(__file__).resolve().parent / "snapshot"


def main() -> None:
    SNAPSHOT.mkdir(exist_ok=True)
    for name in ("wallpapi.db", "embeddings.db"):
        source = sqlite3.connect(f"{(SOURCE / name).as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(SNAPSHOT / name)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        print(f"copied {SOURCE / name} -> {SNAPSHOT / name}")


if __name__ == "__main__":
    main()
