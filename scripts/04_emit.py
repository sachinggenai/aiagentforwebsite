"""Emit every output family from the parsed pages.

Usage:
    python scripts/04_emit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.config import force_utf8, load_config  # noqa: E402
from ams.fetch import iter_cached_pages  # noqa: E402
from ams.pipeline import load_documents, load_shared  # noqa: E402
from ams.emit import api_inventory, chunks, graph, media, qa, tables  # noqa: E402


def main() -> int:
    force_utf8()
    config = load_config()
    documents = load_documents(config)
    shared = load_shared(config)

    if not documents:
        print("No parsed pages found. Run scripts/03_parse.py first.")
        return 1

    raw_html = {url: html for url, html in iter_cached_pages(config)}

    print(f"emitting from {len(documents)} parsed page(s)\n")

    results = {
        "tables": tables.emit(config, documents, shared),
        "chunks": chunks.emit(config, documents, shared),
        "qa": qa.emit(config, documents, shared),
        "graph": graph.emit(config, documents, shared),
        "media": media.emit(config, documents, shared, raw_html),
        "api_inventory": api_inventory.emit(config, documents, raw_html),
    }

    for stage, counts in results.items():
        print(f"{stage}:")
        for key, value in counts.items():
            print(f"    {key:24} {value}")
        print()

    out = config.path("out")
    print(f"outputs written to {out}")
    for path in sorted(out.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(out)}  ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
