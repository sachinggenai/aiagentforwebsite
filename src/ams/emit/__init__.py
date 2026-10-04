"""Output emitters.

Every emitter reads the same canonical ``PageDocument`` set from
``data/pages/`` and writes one output family. Nothing here parses HTML, and no
emitter depends on another — so a fix to one output format cannot change
another, and all of them stay consistent with each other by construction.

Output families:

======================  ==================================================
``tables.py``           CSVs + ``ams.sqlite`` — tabular / text-to-SQL RAG
``chunks.py``           ``chunks.jsonl``       — vector / naive RAG
``qa.py``               ``qa_pairs.jsonl`` + ``voice_short_answers.jsonl``
``graph.py``            ``graph_nodes.jsonl`` + ``graph_edges.jsonl``
``media.py``            ``media_manifest.json``
``api_inventory.py``    ``api_inventory.json`` + ``API_INVENTORY.md``
======================  ==================================================
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    """Write rows as JSON Lines, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def write_json(path: Path, payload: Any) -> None:
    """Write pretty-printed JSON with non-ASCII preserved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
