"""Turn sources into chunks.

Format-dispatched rather than strategy-object-dispatched (CR-01 contract C9.2):

* ``.md`` — split by ``##`` heading, then recursively to ``chunk_size``. This is
  the source behaviour (`rag_legacy.py:184-243`), carried for products whose
  corpus is prose.
* ``.jsonl`` — **one chunk per line, metadata preserved verbatim.** ANITA's
  corpus arrives pre-chunked from its scraper carrying ``chunk_id``,
  ``product_id``, ``chunk_type`` and ``source_url``; citations, the corpus gate
  and the frozen evaluation set all depend on those fields. Re-splitting would
  break all three, so this path never touches the text.

``chunk_size``/``chunk_overlap`` therefore apply only to ``.md`` sources. That
is stated rather than implied, because a reader who assumes they apply
everywhere would expect ANITA's 141 chunks to become some other number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .errors import CorpusGateError


@dataclass(frozen=True)
class SourceChunk:
    """One indexable unit, with the provenance the caller needs to cite it."""

    chunk_id: str
    text: str
    section: str = ""
    source_file: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


def _split_markdown(text: str, *, chunk_size: int, chunk_overlap: int,
                    source_file: str) -> list[SourceChunk]:
    """``##`` sections, then a recursive character split within each.

    The separators are the source's (`rag_legacy.py:239-243`); paragraph
    boundaries are preferred over sentence, sentence over word, so a chunk
    rarely ends mid-thought.
    """
    sections: list[tuple[str, str]] = []
    current_heading, buffer = "", []
    for line in text.splitlines():
        if line.startswith("## "):
            if buffer:
                sections.append((current_heading, "\n".join(buffer).strip()))
            current_heading, buffer = line[3:].strip(), []
        else:
            buffer.append(line)
    if buffer:
        sections.append((current_heading, "\n".join(buffer).strip()))

    chunks: list[SourceChunk] = []
    for heading, body in sections:
        if not body:
            continue
        for piece in _recursive_split(body, chunk_size, chunk_overlap):
            chunks.append(SourceChunk(
                chunk_id=f"{source_file}:{heading}:{len(chunks)}",
                text=piece,
                section=heading,
                source_file=source_file,
                metadata={"doc_type": "prose", "section": heading},
            ))
    return chunks


def _recursive_split(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    for separator in ("\n\n", "\n", ". ", " "):
        if separator not in text:
            continue
        parts, out, buffer = text.split(separator), [], ""
        for part in parts:
            candidate = f"{buffer}{separator}{part}" if buffer else part
            if len(candidate) <= size:
                buffer = candidate
                continue
            if buffer:
                out.append(buffer)
            buffer = part
        if buffer:
            out.append(buffer)
        if out and max(len(p) for p in out) <= size:
            return [p for p in out if p.strip()]
        text = "\n".join(out) if out else text
    # No separator produced pieces small enough — hard-wrap as a last resort.
    return [text[i:i + size] for i in range(0, len(text), max(size - overlap, 1))]


def _read_jsonl(path: Path) -> list[SourceChunk]:
    """One chunk per line, metadata preserved exactly as written."""
    chunks: list[SourceChunk] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorpusGateError(f"{path.name}:{number} is not valid JSON — {exc}") from exc
        text = str(record.get("text") or "").strip()
        if not text:
            continue
        chunk_id = str(record.get("chunk_id") or f"{path.stem}:{number}")
        chunks.append(SourceChunk(
            chunk_id=chunk_id,
            text=text,
            section=str(record.get("section") or ""),
            source_file=path.name,
            metadata={k: v for k, v in record.items() if k not in {"text", "chunk_id"}},
        ))
    return chunks


def load_sources(sources: Iterable[Path], *, chunk_size: int, chunk_overlap: int) -> list[SourceChunk]:
    """Read every source into chunks, dispatching on suffix.

    An unsupported suffix is refused rather than skipped: silently ignoring a
    source would produce an index that is missing a corpus the operator
    believes is loaded.
    """
    chunks: list[SourceChunk] = []
    for path in sources:
        path = Path(path)
        if not path.exists():
            raise CorpusGateError(f"source not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            chunks.extend(_read_jsonl(path))
        elif suffix in {".md", ".markdown", ".txt"}:
            chunks.extend(_split_markdown(
                path.read_text(encoding="utf-8"),
                chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                source_file=path.name,
            ))
        else:
            raise CorpusGateError(f"unsupported source type {suffix!r} for {path.name}")
    return chunks


def validate_corpus(chunks: list[SourceChunk], *, expected_markers: tuple[str, ...],
                    blocked_markers: tuple[str, ...]) -> None:
    """The corpus gate (M-1; source defect B-1).

    Markers are matched case-insensitively as substrings over the concatenated
    corpus. The rule is deliberately blunt: the source's gate hardcoded one
    product's markers and returned ``None`` for any other corpus, which is why
    ANITA could not use it at all. What matters is that acceptance is
    *configured* and that a foreign corpus cannot pass.

    Both sides must hold: at least one expected marker present, and **no**
    blocked marker present. A corpus carrying both is refused — the blocked
    marker is evidence of contamination, and contamination is not outvoted.
    """
    # Text **and** provenance. A chunk's identity is not only its prose: ANITA's
    # marker appears in 0 of 141 chunk texts and 141 of 141 `source_url` values,
    # because the scraper records where a chunk came from and never repeats the
    # domain inside the content. A gate that searched text alone would reject
    # the corpus it exists to accept — which is exactly what it did on the first
    # run against real data.
    haystack = "\n".join(
        f"{c.text}\n{c.section}\n{c.source_file}\n"
        + "\n".join(f"{k}={v}" for k, v in c.metadata.items())
        for c in chunks
    ).casefold()
    blocked = tuple(m for m in blocked_markers if m.casefold() in haystack)
    if blocked:
        raise CorpusGateError("blocked marker present", blocked=blocked)
    if not any(m.casefold() in haystack for m in expected_markers):
        raise CorpusGateError("no expected marker found", missing=tuple(expected_markers))


__all__ = ["SourceChunk", "load_sources", "validate_corpus"]
