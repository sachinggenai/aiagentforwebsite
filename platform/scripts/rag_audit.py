"""Audit the RAG end to end: extracted → chunked → embedded → retrieved → used.

Five questions, each answered by measurement rather than by reading the code:

  1. Did every page yield content?
  2. Did that content become chunks, and what was dropped?
  3. Are all chunks actually embedded in the vector store?
  4. When a query runs, does the dense leg contribute or is it decorative?
  5. How many chat answers use retrieval at all?

The last two are the ones that matter. A corpus can be complete and indexed and
still not be *used* — the deterministic answer engine may be carrying every
question while retrieval never contributes a single result.

Run:  .venv\\Scripts\\python scripts\\rag_audit.py
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CORPUS = PLATFORM.parent / "data" / "out"


def main() -> int:
    print("=" * 96)
    print("RAG AUDIT — extracted → chunked → embedded → retrieved → used")
    print("=" * 96)

    # -- 1. extraction -----------------------------------------------------
    print("\n1. EXTRACTION — did every page yield content?")
    # Parsed pages live beside the raw cache, not under data/out. Reporting 0
    # here would read as "no page yielded anything", which is the opposite of
    # the truth and exactly the kind of wrong-but-green signal this audit
    # exists to catch.
    pages_dir = next(
        (d for d in (CORPUS.parent / "pages", CORPUS / "pages") if d.exists()), None
    )
    pages = sorted(pages_dir.glob("*.json")) if pages_dir else []
    kinds = collections.Counter()
    empty: list[str] = []
    for path in pages:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            empty.append(path.stem)
            continue
        kinds[doc.get("page_type", "?")] += 1
        if not (doc.get("sections") or doc.get("structured") or doc.get("faqs")):
            empty.append(path.stem)
    print(f"   pages parsed : {len(pages)}")
    for kind, count in kinds.most_common():
        print(f"      {kind:20} {count}")
    print(f"   pages with no extracted content: {len(empty)} {empty[:4]}")

    # -- 2. chunking -------------------------------------------------------
    print("\n2. CHUNKING — what became chunks, and what did not?")
    chunks = [json.loads(line) for line in (CORPUS / "chunks.jsonl").open(encoding="utf-8")]
    by_type = collections.Counter(c.get("chunk_type") for c in chunks)
    print(f"   chunks       : {len(chunks)}")
    for kind, count in by_type.most_common():
        print(f"      {kind:28} {count}")

    chunked_products = {c.get("product_id") for c in chunks if c.get("product_id")}
    sqlite_products = set()
    import sqlite3
    conn = sqlite3.connect(CORPUS / "ams.sqlite")
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT product_id FROM products"):
        sqlite_products.add(row["product_id"])
    missing = sorted(sqlite_products - chunked_products)
    print(f"   products in the catalogue : {len(sqlite_products)}")
    print(f"   products with chunks      : {len(chunked_products)}")
    print(f"   products with NO chunks   : {len(missing)} {missing[:6]}")

    # What content exists in the DB that never became a chunk?
    spec_rows = conn.execute("SELECT COUNT(*) FROM product_specs").fetchone()[0]
    faq_rows = conn.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]
    covered_specs = sum(1 for c in chunks if c.get("chunk_type") == "product_specs")
    covered_faqs = sum(1 for c in chunks if c.get("chunk_type") == "product_faq")
    print(f"\n   specification rows in the DB : {spec_rows}")
    print(f"   chunks carrying them         : {covered_specs} "
          f"(each chunk holds a whole spec list)")
    print(f"   FAQ pairs in the DB          : {faq_rows}")
    print(f"   FAQ chunks                   : {covered_faqs} "
          f"({'complete' if covered_faqs == faq_rows else 'MISMATCH'})")

    # -- 3. embedding ------------------------------------------------------
    print("\n3. EMBEDDING — are all chunks in the vector store?")
    from anita.assembly import assemble, deployed_environment

    built = assemble(require_preflight=False, env=deployed_environment())
    chat = built.chat
    retriever = chat.retriever
    if retriever is None:
        print("   NO RETRIEVER WIRED — every retrieval question declines")
        return 1

    indexed = retriever.build_index()
    store = retriever.store
    print(f"   chunks loaded  : {len(retriever.chunks)}")
    print(f"   chunks indexed : {indexed}")
    print(f"   store contains : {store.count()}")
    ok = store.count() == len(retriever.chunks)
    print(f"   complete       : {ok}")
    if not ok:
        print(f"   MISSING        : {len(retriever.chunks) - store.count()} chunks")

    # -- 4. does the dense leg actually contribute? ------------------------
    print("\n4. RETRIEVAL — does the dense leg contribute, or only BM25?")
    probes = [
        "what is the ultrasound gel used for",
        "how do I store the cassette",
        "X-Ray film developing hanger",
        "contrast media compatibility",
        "what is the price of the ultrasound gel?",
    ]
    dense_wins = sparse_wins = neither = 0
    for query in probes:
        result = retriever.retrieve(query)
        if not result.chunks:
            neither += 1
            print(f"   {query[:44]:46} gate refused ({result.decision.reason})")
            continue
        top = result.chunks[0]
        from_dense = top.dense_score > 0
        from_sparse = top.sparse_score > 0
        if from_dense and from_sparse:
            dense_wins += 1
        elif from_sparse:
            sparse_wins += 1
        print(f"   {query[:44]:46} top={top.chunk.chunk_type:16} "
              f"dense={top.dense_score:.3f} sparse={top.sparse_score:.2f}")
    print(f"\n   top hit carried a dense score : {dense_wins}/{len(probes)}")
    print(f"   top hit was sparse-only       : {sparse_wins}/{len(probes)}")
    print(f"   gate refused                  : {neither}/{len(probes)}")

    # -- 5. is retrieval used by chat at all? ------------------------------
    print("\n5. USAGE — how many chat answers actually consult retrieval?")
    calls: list[str] = []
    original = retriever.retrieve

    def traced(query, **kw):
        calls.append(query)
        return original(query, **kw)

    retriever.retrieve = traced

    questions = [
        "what is the price of the ultrasound gel?",
        "what is the minimum order quantity for the gel?",
        "tell me about the X-Ray Cassette",
        "compare the Ultrasound Gel and the X-Ray Cassette",
        "where is your office?",
        "Show me the catalogue",
        "what can you do",
        "hello",
        "who won the world cup?",
        "what is your phone number?",
    ]
    for question in questions:
        calls.clear()
        body = chat.post_message(message=question, session_id=f"audit-{question[:6]}").body
        used = "retrieval" if calls else "deterministic/other"
        print(f"   {question[:44]:46} {used:20} intent={body.get('intent')}")

    retriever.retrieve = original

    print("\n" + "=" * 96)
    print("  A corpus that is complete and indexed is not the same as a corpus")
    print("  that is used. Section 5 is the answer to 'is the RAG doing the work'.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
