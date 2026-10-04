# CR-01 — Shared-module contract (binding on all artifacts in this change)

> **Lens:** Architect · **Decided by:** user (Q7→a, Q8→b, 2026-10-03) · **Inputs:** CR-01 §1–2, Appendix A, Appendix B
> **Status:** FROZEN — every HLD, LLD and story in this change must conform to this contract.

## C1. Why a contract file exists

Three artifacts are authored in parallel (architecture + two story sets). Without a frozen
interface they drift. This file is that interface. If an artifact needs to deviate, it raises a
`CV-xx` contradiction rather than silently diverging.

## C2. Decision record

| Decision | Choice | Rationale |
|---|---|---|
| Q7 sentiment | **(a)** shared parameterised core; escalation is part of the core | Only option satisfying CR-01.1 + CR-01.3 while keeping UC-10. Side A has no escalation at all (A-1) |
| Q8 RAG | **(b)** adopt the code; lift the corpus gate and retarget vocabulary | Closest honest reading of "same code". The corpus gate is a validation constant, not a design decision (B-1) |
| External ERC service | **NOT adopted** in this change | Adds a third repository + network hop for 141 chunks. Deferred, not rejected — see C7 |

## C3. The two shared modules

Both live in a new top-level package, imported by either product. Neither may import from
`anita.*` or from universityDemo's `app.*`.

```
shared/
  rag/          # from universityDemo app/rag.py + rag_legacy.py, generalised
  sentiment/    # from universityDemo app/sentiment/ + anita/sentiment.py, merged
```

### C3.1 `shared/rag` — required interface

```python
class RagConfig:                      # ALL of these are injected, none read at import
    collection_name: str              # was RAG_COLLECTION_NAME (import-time os.environ — B-5)
    persist_path: Path                # was CHROMA_DB_PATH
    embed_model: str
    sources: tuple[Path, ...]         # was a single hardcoded Meridian path (B-1)
    expected_markers: tuple[str, ...] # corpus validation — was ["meridian university"]
    blocked_markers: tuple[str, ...]  # corpus rejection — admission gate, generalised
    system_prompt: str                # was Meridian-specific (B-3)
    refusal_text: str                 # was "…in the university profile." (B-3)
    top_k: int; fetch_k: int; chunk_size: int; chunk_overlap: int

class RagService:
    def __init__(self, config: RagConfig, *, mcp: McpClient | None = None) -> None: ...
    def retrieve(self, query: str) -> RetrievalResult: ...      # rung recorded
    def build(self) -> int: ...                                 # returns vectors written
    def status(self) -> dict: ...                               # breaker + rung, for /api/perf/policy
```

**Carried over unchanged (proven in source):** circuit breaker `closed|half_open|open`
(`rag_mcp.py:348-383`), probe single-flight + abandoned-probe recovery (`:424-463`), rung
recording `mcp|local|none` (`rag.py:194-216`), Chroma `hnsw:space=cosine`
(`rag_legacy.py:259`), RRF k=60 fusion (`rag_legacy.py:410-459`).

### C3.2 `shared/sentiment` — required interface

```python
class SentimentConfig:                # injected, no import-time globals (A-4)
    w1: float = 0.30; w2: float = 0.30; w3: float = 0.25; w4: float = 0.15
    ewma_lambda: float = 0.35
    negative_threshold: float = -0.20   # ONE definition; today duplicated in 4 places
    extraction_prompt: str              # caller supplies domain vocabulary
    objection_labels: tuple[str, ...]   # caller supplies
    escalation_enabled: bool = True

class SentimentCore:
    def score_turn(self, message: str, *, lead_id: str = "") -> (Extraction, CompositeScore): ...
    def categorise(self, composite, extraction) -> Category: ...
    def should_escalate(self, composite, extraction) -> tuple[bool, str]: ...
```

**Capabilities that MUST survive (both are ANITA additions absent from Side A):**
- rule-based fallback on LLM failure (A-2)
- `should_escalate` + `on_escalate` (A-1, UC-10)

**Defect to fix during extraction, not port:** the `limit=10` vs `>=100` gate (A-3).

## C4. Brownfield constraint — ANITA's existing behaviour is preserved

ANITA's current answer path is **deterministic-first**: 8 of 10 questions are answered from SQL
(`AnitaAnswers`) and never touch retrieval. **This change does not alter that.** `shared/rag`
replaces the retrieval leg only; `ChatSurface`, `GroundingGate`, `AnitaAnswers` and the
orchestrator keep their contracts.

The **`GroundingGate` is retained** — universityDemo has no equivalent (B.3). It stays in
`anita.retrieval` and wraps the shared retriever.

## C5. Migration obligations (every story set must cover these)

| # | Obligation | Source |
|---|---|---|
| M-1 | Corpus gate becomes config, not a constant | B-1 |
| M-2 | Collection name from config, read per-instance not at import | B-5 |
| M-3 | No process-global env mutation on import | B-4 |
| M-4 | BM25 cache keyed by collection, not the literal `"bm25"` | B-6 |
| M-5 | Prompts and refusal strings parameterised | B-3 |
| M-6 | Sentiment config actually read (today `SentimentSettings` is inert) | A.2 |
| M-7 | `negative_threshold` collapsed from four copies to one | A.2 |
| M-8 | ANITA's escalation and fallback preserved through extraction | A-1, A-2 |
| M-9 | `limit=10` vs `>=100` defect fixed | A-3 |

## C6. Definition of done for this change

- Both modules import with **no** `anita.*` or universityDemo `app.*` imports
- `system_check.py` UC-10 moves from DEFERRED to PASS (escalation has a sink)
- ANITA's 614 platform tests still pass; the release gate at 70 cases / 25 scenarios still passes
- Chroma persistence: restart no longer re-embeds; index survives
- A second consumer can instantiate both modules with its own config and no code edits

## C7. Explicitly out of scope

- The external `enterprise-rag-core` service and MCP-first-over-HTTP (Q8≠c)
- PostgreSQL persistence for sentiment (ANITA has no Postgres; the core exposes a store Protocol)
- Voice, WhatsApp, outbound (still deferred per `anita.yaml`)

---

# C8. Contradiction resolutions (CV register)

Raised by the architecture pass. Each is resolved here; the contract above is amended by this
section, which takes precedence where the two differ.

## CV-04 — Corpus size: plan says 169, reality is 141 — **RESOLVED: plan is wrong**

Verified: `wc -l data/out/chunks.jsonl` → **141**. The frozen plan states **169** in
`00-product-intent.md:5`, `:133`, `01-brd.md:15`, `:147` and further places.

Cause: the chunker changed after those figures were written — specs are now rebuilt from
`doc.specs` rather than the page section, and the `Q:`/`A:` labels were removed. The count moved
139 → 141 and the plan was never re-synced.

**Action:** correct every stale figure in the frozen plan as part of this change (a documentation
fix, no behaviour change). **A gate written against 169 would refuse ANITA's own 141-chunk corpus
— the same failure mode as B-1.**

## CV-05 — `RetrievalResult` has no counterpart in the source — **RESOLVED: design the façade**

The source returns `str` (`rag.py:112`, `:104`); no result class exists. The contract's
`retrieve -> RetrievalResult` is therefore new work, not a port.

**Resolution:** `shared/rag` exposes `RetrievalResult` (chunks + rung + breaker state + scores).
universityDemo's string call sites are preserved by a thin `retrieve_text(query) -> str` shim that
renders the same `[§ section]\nbody` shape (`rag_legacy.py:485-491`). `build()` raises on corpus
rejection; the source's `None` return is **not** carried over — a silent `None` is how B-1 would
pass unnoticed.

## CV-06 — The breaker has no failure source without MCP — **RESOLVED: explicit degeneracy**

Real, and the same defect class as B-12. With `mcp=None` the `mcp` rung is unreachable and the
breaker never leaves `closed`.

**Resolution:** when no transport is injected, the rung set is **`local|none`**, the breaker is
never consulted, and `status()` reports `breaker: "not_applicable"`. This degeneracy must be
**explicitly tested** — asserting the breaker is *absent by design*, not silently inert. Carrying
a mechanism that cannot fire is precisely the failure this session has found nine times.

## CV-07 — Sentiment core must inject a client, not assume one — **RESOLVED**

`SentimentConfig` gains `client: ExtractionClient | None`. `ExtractionClient` is a Protocol with a
single `extract(prompt: str) -> str` method. `score_turn` stays **sync**; an async implementation
wraps it. When `client is None`, extraction uses the rule-based path — which is ANITA's fallback
and now also the shared core's zero-dependency mode. universityDemo injects its async Ollama client.

## CV-08 — ANITA's corpus is pre-chunked — **RESOLVED: injected strategy**

ANITA's chunks arrive from the scraper with `chunk_id`/metadata that citations, the corpus gate
and the frozen eval set depend on. Re-chunking would break all three.

**Resolution:** `RagConfig` carries `chunking: ChunkingStrategy`. Two implementations:
`RecursiveChunking` (the source's `RecursiveCharacterTextSplitter` behaviour) and
`PassThroughChunking` (ANITA's JSONL, metadata preserved verbatim). No raw-text re-split for ANITA.

## CV-09 — Fusion parity is NOT free — **RESOLVED: this invalidates the no-regression claim**

The most important finding. The source's fusion is **unweighted and keyed on document text**
(`rag_legacy.py:438-453`); ANITA's is **weighted 0.65/0.35, keyed on chunk index**, and adds a
**lexical-override rule** (`retrieval.py:206-235`, `:648-674`). Adopting the source fusion
unchanged **would change ANITA's rankings** — so "no answer regression" is not automatic.

**Resolution:** `RagConfig` carries `fusion: FusionPolicy` (weights + k + lexical override).
ANITA keeps its existing policy; the source policy is available for other consumers. **The
migration story must include a ranking-parity test** comparing the top-k before and after, and
must not claim no-regression without it.

## Corrections to previously recorded facts

| Claim | Correction | Evidence |
|---|---|---|
| "No Chroma-backed store exists" (CR-01 Appendix B) | **Wrong.** `ChromaStore` exists at `platform/app/anita/stores.py:41`, with a dimension guard and a collection-name assertion — **imported by nothing** | `grep ChromaStore` → only a `.pyc` |
| "chromadb is in requirements but not installed" | Correct, and confirmed: declared `platform/requirements.txt:24`, absent from the venv | import check |
| "ANITA's corpus gate is structural" | **Wrong** — it is already marker-based: `REQUIRED_CORPUS_MARKER = "anitamedicalsystems"`, `FORBIDDEN_CORPUS_MARKERS = ("meridian", "university of maryland", "admissions")` at `index.py:75-76`, mapping one-to-one onto the contract's shape | `index.py:75-76` |

**Consequence for US-103:** the Chroma persistence story is **partly built already** —
`ChromaStore` needs wiring and testing, not authoring. Effort estimate drops materially.

---

# C9. CV register reconciliation

Two authors ran in parallel and each allocated `CV-xx` independently. **They raised the same two
issues under different numbers** — corroboration, not collision — and the numbering must be
reconciled before coding so a story and an architecture note do not appear to disagree.

| Architecture pass | RAG story pass | The issue | Status |
|---|---|---|---|
| The `mcp` seam: the contract requires it (C3.1), C7 bans MCP-over-HTTP | **Resolved** — see C9.1 | story-pass label, superseded by CV-06 |
| Pre-chunked JSONL corpus vs the contract's markdown-splitter model | **Resolved** — see C9.2 | story-pass label, superseded by CV-08 |

**Canonical numbering from here: `CV-04`…`CV-09` are the architecture pass.** The RAG story pass's
the story pass’s first two labels are **superseded** for CV-06 and CV-08 respectively. Story files carrying
the old labels remain valid in substance; they are corrected by this mapping.

## C9.1 — `mcp` seam: Protocol-only is sufficient — CONFIRMED

The story pass resolved this by defining `McpClient` as a **Protocol only**, shipping no
implementation, with `mode_effective: "off"` and rungs `local|none`.

**This satisfies C3.1.** The contract requires the *seam* so a transport can be injected later
without a signature change; it does not require a transport to exist. C7's ban is on adopting the
external ERC service, not on the seam that would carry it.

**It is also exactly the CV-06 resolution**: with no transport, the rung set is `local|none`, the
breaker is never consulted, `status()` reports `breaker: "not_applicable"`, and that degeneracy is
**explicitly tested** — asserting the breaker is absent by design rather than silently inert.

## C9.2 — Pre-chunked JSONL — CONFIRMED, with the contract field set unchanged

The story pass kept the contract's field set byte-identical and treats `.jsonl` entries as one
chunk per line, with `chunk_size`/`chunk_overlap` applying only to `.md` sources.

**This is the CV-08 resolution** (`ChunkingStrategy` injected; `PassThroughChunking` for ANITA).
Implementing it as format-dispatch rather than as a strategy object is an acceptable reading and
is **accepted** — it preserves the contract surface and produces the same behaviour.

## CV-10 — `claim_probe` is non-atomic

Check-then-set with no lock (`rag_mcp.py:448-463`); two callers can both claim the probe.
Fixed in US-104 under one lock, with a 20-thread single-flight test. Raised by the RAG story pass.

## CV-11 — No embedder seam

`RagService.__init__(config, *, mcp=None)` leaves the shared package resolving ANITA's
`OllamaEmbedder` itself — a product dependency inside shared code. `embedder=None` added as a
second keyword-only seam. Raised by the RAG story pass.

## C9.3 — New defects raised by the story pass (accepted)

| # | Finding | Action |
|---|---|---|
| **CV-10** | **`claim_probe` is non-atomic** — check-then-set with no lock (`rag_mcp.py:448-463`). Under concurrency two callers can both claim the probe | Accepted. Fixed in US-104 under one lock with a **20-thread single-flight test**. This is beyond the B-8/B-9/B-10 fixes and was **not** carried silently |
| **CV-11** | **No embedder seam in `RagService.__init__`.** The contract gives `(config, *, mcp=None)`, so the shared package would have to resolve ANITA's `OllamaEmbedder` itself — a product dependency inside a shared module | Accepted. **`embedder=None` is added as a second keyword-only seam**, alongside `mcp`. `mcp` stays exactly as contracted. Signature addition is permitted where it *removes* a product dependency from the shared module |

## C9.4 — Story-set status

| Story | Touches ANITA code? | Gate obligation |
|---|---|---|
| US-101 shared `rag` extraction | No | 614 tests untouched |
| US-102 configurable corpus gate | No | 614 tests untouched |
| US-103 Chroma persistence | No | 614 tests untouched |
| US-104 breaker + rungs | No | 614 tests untouched |
| **US-105 ANITA adapter** | **Yes** — live wiring | **Must run the full suite and the 70-case/25-scenario gate on BOTH backends** |

Only US-105 changes live behaviour, so only US-105 carries the regression burden. Rollback is
`ANITA_RAG_BACKEND=memory`, which constructs exactly today's `Retriever` + `InMemoryStore` graph.

---

# C10. Third CV register + final reconciliation

Three authors ran in parallel. **Each allocated the same first label independently** — three different issues
under one number. Canonical register below; the story-file labels are superseded by this mapping.

| Canonical | Raised by | Issue | Status |
|---|---|---|---|
| Architecture | Plan says 169 chunks; reality is 141 | Resolved — plan correction task | CV-04 |
| Architecture | `RetrievalResult` has no source counterpart | Resolved — C8 | CV-05 |
| Architecture **and** RAG stories (as "CV-01") | `mcp` seam vs the C7 ban | Resolved — C9.1, Protocol-only confirmed | CV-06 |
| Architecture | Sentiment core must inject a client | Resolved — C8 | CV-07 |
| Architecture **and** RAG stories (as "CV-02") | Pre-chunked JSONL vs splitter model | Resolved — C9.2 | CV-08 |
| Architecture | Fusion parity is not free | Resolved — `FusionPolicy` + ranking-parity test | CV-09 |
| RAG stories | `claim_probe` non-atomic | Resolved — US-104, one lock, 20-thread test | CV-10 |
| RAG stories | No embedder seam | Resolved — `embedder=None` seam | CV-11 |
| **CV-12** | **Sentiment stories (story-pass label, superseded)** | **M-7 said "one definition"; the YAML key is operator-tunable** | **Resolved below** |

## CV-12 — M-7 was imprecise — RESOLVED: one default, override intact

M-7 said `negative_threshold` collapses "from four copies to one". But `anita.yaml:94` exposes
`NEGATIVE_ESCALATION_THRESHOLD` as an **operator-tunable key** (loader `config.py:401-412`).

**My M-7 was loosely worded.** The defect was never that the value was configurable — it was that
**four independent literals could drift**: `config.py:211`, `sentiment.py:348`, `sentiment.py:373`,
`sentiment.py:429`.

**Resolution confirmed as implemented:** a single `DEFAULT_NEGATIVE_THRESHOLD: float = -0.20` in
`shared/sentiment/config.py`, imported by `platform/app/anita/config.py`. The YAML/env key
**remains** as the operator override that feeds it. One default, one override path, zero
independent literals.

**The key is NOT frozen.** An operator must be able to tune the escalation threshold without a code
change — that is the point of it being in `anita.yaml`, and freezing it would be a regression in
operability disguised as tidiness.

## C10.1 — Accepted decisions where the contract was silent

Recorded as decisions, not deviations:

| Decision | Rationale |
|---|---|
| `kw_only=True` on the config dataclass | Legalises the contract's field order without reordering the frozen interface |
| `p_convert` / `predictive_layer_active` / `trajectory` carried on `CompositeScore` | Keeps `categorise(composite, extraction)` at the exact contracted signature |
| Additive `ascore_turn` for async consumers | universityDemo is async throughout; the sync `score_turn` stays as contracted |
| `escalation_enabled` gates **dispatch**, never the **decision** | A suppressed escalation is still *recorded* — silence must not erase the evidence |
| `store` is a constructor object, never named in YAML | A store is an injected dependency, not configuration |
| `assert_store_conforms` in `shared/sentiment/testing.py` | Makes the second-consumer DoD mechanically checkable |

## C10.2 — Explicit Unknowns (left as gaps, not guessed)

| # | Unknown | Handling |
|---|---|---|
| U-1 | UC-10 scenario A1 "buyer declines a human" needs a **decline intent** that does not exist in the evidence | Deferred. `escalation_enabled` is the operator equivalent for now |
| U-2 | Whether `MIN_LABELED_OUTCOMES` should be operator-tunable | Raised in US-113; currently a code default of 100 |
| U-3 | Side A's exact BANT completeness rule | Raised in US-111; not in the evidence |

## C10.3 — Self-corrections by the authors (recorded, not hidden)

- The **sentiment author caught its own first draft violating CV-07** — it had given `SentimentCore`
  a `scorer` constructor kwarg, contradicting the frozen `config.client: ExtractionClient | None`.
  US-111 and US-115 were edited to conform, with T-02 asserting the contracted shape.
- The **architecture author re-verified every citation** and corrected two of its own line ranges.
- The **sentiment author raised an erratum against Appendix A**: `non_fit_keywords` is **7 vs 3**,
  not "6 vs 3" as I wrote. **My artifact was wrong and is corrected.**

## C10.4 — Change set complete

| Artifact | Lines |
|---|---|
| `CR-01-rag-sentiment-rearchitecture.md` (+ Appendices A, B, erratum) | 262 |
| `CR-01-contract.md` (C1–C10) | 320 |
| `CR-01-architecture.md` | 314 |
| `CR-01-modularization-delta.md` | 192 |
| `stories/US-101…US-105` (RAG) | 5 files |
| `stories/US-111…US-115` (sentiment) | 5 files |

**12 contradictions raised and resolved. 3 explicit Unknowns. 3 author self-corrections.** No
contradiction was silently absorbed, and no claim about existing behaviour lacks a `file:line`.
