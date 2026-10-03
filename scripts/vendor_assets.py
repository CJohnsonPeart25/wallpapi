"""Fetch the vendored front-end files into `src/wallpapi/web/static/`, byte for byte.

    uv run python scripts/vendor_assets.py

Each file is pinned to a release and a SHA-256, and this script is the only place either is written down.
Upgrading one means changing its URL and hash here and running the script. Every file is downloaded and
checked before any is written, so a hash that does not match leaves the folder as it was.
`.gitattributes` keeps checkout from converting their line endings, so the file served is the file fetched.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx2

STATIC = Path(__file__).resolve().parent.parent / "src" / "wallpapi" / "web" / "static"


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    url: str
    sha256: str


ASSETS = (
    Asset(
        name="htmx.min.js",
        url="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js",
        sha256="e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447",
    ),
    Asset(
        name="pico.indigo.min.css",
        url="https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.indigo.min.css",
        sha256="3ff75cde84c76491549e1a7c64294c2c83cb2f92231ea44692f9ffc69897a811",
    ),
    Asset(
        name="alpine.min.js",
        url="https://cdn.jsdelivr.net/npm/alpinejs@3.17.4/dist/cdn.min.js",
        sha256="232519394c6c8fdba6f362b1d9da16106db513cdbf899011f00daab4051df31c",
    ),
)


def main() -> int:
    fetched: dict[str, bytes] = {}
    with httpx2.Client(timeout=30.0, follow_redirects=True) as client:
        for asset in ASSETS:
            response = client.get(asset.url)
            response.raise_for_status()
            digest = hashlib.sha256(response.content).hexdigest()
            if digest != asset.sha256:
                print(
                    f"{asset.name}: expected {asset.sha256}, got {digest}; nothing written", file=sys.stderr
                )
                return 1
            fetched[asset.name] = response.content
    for name, content in fetched.items():
        (STATIC / name).write_bytes(content)
        print(f"{name}: {len(content):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
