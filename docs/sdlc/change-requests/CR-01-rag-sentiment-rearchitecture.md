# CR-01 — Re-architect RAG and Sentiment to the universityDemo pattern

> **Lens:** PO (scope) · **Decided by:** user, 2026-10-03 · **Inputs:** `plan-state.md`, `07-brownfield-reconciliation.md`
> **Status:** IN ANALYSIS — evidence gathering in flight

## 1. The decision (user-provided)

> "I want to follow same architecture and code as UniversityDemo, as RAG and Sentiment
> analysis are stand-alone re-usable modules [they] should be used here as is or better way.
> Use AI SDLC skill to plan and complete the work carefully this time without missing any
> details, no your own assumptions."

Decomposed into three separable requirements:

| # | Requirement | Notes |
|---|---|---|
| CR-01.1 | ANITA's **RAG** adopts universityDemo's architecture and code | Architecture parity, not just outcome parity |
| CR-01.2 | ANITA's **Sentiment** adopts universityDemo's architecture and code | Same |
| CR-01.3 | Both become **standalone, reusable modules** usable by either product | This is the substantive constraint — it decides *how* the code is extracted, not just copied |

**CR-01.3 is the hard one.** "Reusable by either product" means neither product's vocabulary,
paths, collection names, config keys or corpus assumptions may be baked in. A straight copy
satisfies CR-01.1/2 and fails CR-01.3.

## 2. Constraint (user-provided)

**No assumptions.** Every claim entering the plan must be verified against the actual code in
both repositories. The Evidence Register in the resulting analysis carries a citation for each
material fact; anything unverifiable is recorded as an explicit gap, not filled in.

## 3. Superseded guidance

The earlier assessment (chat, 2026-10-03) recommended **against** adopting the MCP-first
topology, on the grounds that universityDemo runs a separate MCP service, circuit breaker and
fallback ladder in front of **35 vectors**, while ANITA has **141 chunks** — i.e. the existing
architecture is already over-built for the data it serves.

**That recommendation is superseded by user decision.** It is recorded here because the
reasoning may still be useful (e.g. whether ANITA adopts MCP-first or the module boundary
without the separate process), but it is not a blocker and will not be re-litigated.

## 4. Verified facts so far

| Fact | Evidence |
|---|---|
| universityDemo RAG = `app/rag.py` 426 L, `app/rag_mcp.py` 526 L, `app/rag_legacy.py` 588 L | `wc -l`, code-observed |
| universityDemo MCP service = `app/mcp/server.py` + `app/mcp/tools.py` | directory listing, code-observed |
| universityDemo Chroma collection `langchain` holds **35 vectors** | `chromadb.PersistentClient(...).list_collections()`, code-observed |
| universityDemo store path `./chroma_local_db` | `.env` (name only), code-observed |
| ANITA retrieval = `platform/app/anita/retrieval.py` — `Chunk`, `MiniBM25`, `GroundingGate`, `Retriever`, `InMemoryStore`, `weighted_rrf` | code-observed |
| ANITA holds **141 chunks**, `chromadb` NOT installed, store in use = `InMemoryStore` | runtime check, code-observed |
| ANITA sentiment = `platform/app/anita/sentiment.py`, referenced by no surface | import-reachability scan, code-observed |

## 5. Open questions (to be closed by evidence, not assumption)

- **Q1** What is universityDemo's public RAG interface, exactly — signatures, and what must a
  caller supply? → in flight
- **Q2** Is the MCP service separable from the retrieval logic, or is retrieval *defined* by
  the MCP hop? → in flight
- **Q3** What makes `rag.py` non-reusable today (hardcoded collection, paths, vocabulary)? → in flight
- **Q4** Are the two sentiment implementations the same code, adapted, or independent? → in flight
- **Q5** Is universityDemo's sentiment on a live path, and is ANITA's genuinely orphaned? → in flight
- **Q6** Does "reusable module" mean a shared package imported by both, or a copy per product? → **needs user decision if evidence is ambiguous**

## 6. Impact on the frozen plan

The Stage 5 gate froze with `STATUS: COMPLETE`. This change touches:

| Artifact | Impact |
|---|---|
| `05-modularization.md` (MOD-01…MOD-10) | MOD-03 (retrieval/grounding) and MOD-07 (sentiment) boundaries change if the modules become shared |
| `06-architecture.md` | Orchestration pattern for MOD-03 changes if MCP-first is adopted |
| `modules/MOD-03`, `modules/MOD-07` | TRD halves need rewriting against the new interfaces |
| `07-brownfield-reconciliation.md` | New `REC-xx` entries for the re-adopted source code |
| `stories/US-*` | Stories touching retrieval or sentiment need re-pointing |

**Q6 is the load-bearing question.** If both products import one shared package, the module
boundary moves from MOD-03/MOD-07 to a new shared-services boundary, and MOD-03/MOD-07 become
thin adapters. If each product keeps a copy, MOD-03/MOD-07 keep their boundaries and only their
internals change. The plan differs substantially between those two answers.

## 7. Next action

Await the two evidence reports (universityDemo RAG; sentiment both sides), then:
1. Resolve Q1–Q5 from evidence
2. Put Q6 to the user with the evidence in hand — it is a scope decision, not a technical one
3. Write the stage artifacts for this change with citations, and update `plan-state.md`

---

# Appendix A — Sentiment: verified evidence (code-observed)

## A.1 Side A — `universityDemo/app/sentiment/` (1,239 LOC, 5 files)

`scorer.py` 694 · `models.py` 249 · `categorizer.py` 220 · `schema.py` 59 · `__init__.py` 17

| Property | Finding | Citation |
|---|---|---|
| Classification | **Model call.** No lexicon, no regex scoring, no fallback | no `re.compile`/`rule_based` anywhere in the package |
| Failure mode | LLM failure → neutral `ScoreResult`. **No degraded path** | `scorer.py:197` |
| Async | Yes, throughout | `scorer.py:126`, `:230`, `:398` |
| Persistence | PostgreSQL — tables + extra `leads` columns | `models.py`, `schema.py` |
| Composite | `W1*s_call + W2*d + W3*i_intent − W4*friction` | `scorer.py:258` |
| Weights | 0.30 / 0.30 / 0.25 / 0.15, `EWMA_LAMBDA` 0.35 | `config.py:490-496` |
| **Escalation** | **None. No function, no callback, no notification** | absence verified across `app/` |
| Nearest artefact | a **display string** only | `categorizer.py:42` |

### Verified defect in Side A

`scorer.py:272` fetches history with `limit=10`; `scorer.py:276` gates the predictive block on
`len(history) >= settings.MIN_LABELED_OUTCOMES` (default **100**). A 10-row fetch cannot satisfy a
100-row gate, so `p_convert` is **always `None`** and `predictive_layer_active` **always `False`**.
`categorize` substitutes `p = 0.0` when `p_convert is None` (`categorizer.py:109`) and **Hot requires
`p >= 0.55`** (`categorizer.py:112`) — so **Hot is unreachable through `compute_lead_score`**.
It survives only via the session-aggregate path (`scorer.py:628`).

### Reuse blockers (all verified)

- **University vocabulary, hardcoded**: prompt at `scorer.py:93-121` ("university admissions team",
  "student", "enroll/apply/join/admission"); `SOFT_OBJECTIONS`/`HARD_OBJECTIONS` at
  `categorizer.py:75-78`; `non_fit_keywords` at `categorizer.py:90-91` — **duplicated and already
  drifted** from a second copy at `categorizer.py:159` (**7 entries vs 3** — corrected; this
  artifact first said 6).
- **Import-time global state**: `settings = Settings()` + `load_dotenv` run at import
  (`config.py:19`, `config.py:659`); `W1..W4` are module-level names (`scorer.py:47-50`), so
  re-tenanting is a global mutation. `scorer.py:44-45` documents this as a known hazard.
- **Coupling**: imports `app.config`, `app.llm_backend`, `app.sentiment.models`,
  `app.sentiment.categorizer`.
- **Dependency**: `psycopg2` (lazy, `models.py:37`).

## A.2 Side B — `anita/sentiment.py` (516 LOC)

| Property | Finding |
|---|---|
| Origin | **Adapted-from Side A — the file says so** | `sentiment.py:3-4` |
| Sync/async | Synchronous (Side A is async) |
| Persistence | **None** — `self._leads: dict` (`sentiment.py:435`), lost on restart |
| Rule-based fallback | **Added** — 5 regexes + 8 objection patterns (`sentiment.py:140-213`) |
| Escalation | **Added** — `should_escalate` (`sentiment.py:369`) + `on_escalate` callback |
| Dropped | BANT, trajectory, `p_convert` |
| Config | **Reads none.** `SentimentSettings` (6 keys) is entirely inert — no `settings`/`os.environ` reference |
| Dead code | `Category.DISQUALIFIED` never returned |
| Callers | **None.** Only `tests/test_mod07_sentiment.py` |

Same weights and EWMA lambda as Side A. No line copied verbatim; identifiers renamed throughout.

## A.3 The finding that changes the request

The instruction was to use these modules "as is". **For sentiment, "as is" is not viable**, on
evidence:

| # | Consequence of adopting Side A verbatim | Severity |
|---|---|---|
| A-1 | **ANITA loses escalation entirely.** Side A has no escalation path at all; ANITA's is `should_escalate` + `on_escalate`. ANITA's **UC-10 is "Negative sentiment detected"** — the capability would be deleted, not ported | **Blocking** |
| A-2 | **ANITA loses the rule-based fallback.** Side A has none, so any LLM outage yields neutral scores silently | High |
| A-3 | **ANITA inherits a verified defect** — Hot unreachable via `compute_lead_score` | High |
| A-4 | **Side A is not reusable as-is.** University vocabulary, import-time globals, `app.config`/`app.llm_backend` coupling, PostgreSQL | Blocking for CR-01.3 |
| A-5 | ANITA gains persistence + async + trajectory + BANT + `p_convert` | **Genuine gain** |

**A-1 and A-4 are the load-bearing findings.** They mean the honest reading of "same architecture
and code" for sentiment is: **adopt the architecture, extract the shared core, keep ANITA's added
capabilities** — not a verbatim copy.

**Q7 (new, for the user):** A-1 is a capability loss, not an implementation detail. Adopting Side A
verbatim deletes UC-10. Options: (a) extract a shared sentiment core that both products parameterise,
keeping escalation in ANITA; (b) adopt Side A and re-add escalation as an ANITA-side extension;
(c) verbatim copy and accept UC-10 becomes unreachable. **Recommendation: (a)** — it is the only one
that satisfies CR-01.1 *and* CR-01.3 *and* keeps UC-10.

---

# Appendix B — RAG: verified evidence (code-observed)

## B.0 The finding that reframes everything

**`app/mcp/server.py` and `app/mcp/tools.py` are NOT the RAG MCP server.** They expose seven
**lead-management/CRM** tools (`app/mcp/tools.py:19-86` → `app/leads/mcp_tools`).

The RAG "MCP" is a **client** — `app/rag_mcp.py` — speaking streamable HTTP to an **external service
in a different repository**: `enterprise-rag-core` (`start_services.ps1:551-564`).
`app/rag_mcp.py:1-5` states it deliberately does not import the MCP SDK at all.

**Consequence:** adopting "the MCP part" by directory name adopts the wrong subsystem. And
MCP-first is not a local pattern — it is a **third-repository runtime dependency**.

## B.1 Blockers to using this code "as is"

| # | Blocker | Evidence |
|---|---|---|
| **B-1** | **The vector-store builder REJECTS any non-Meridian corpus.** `EXPECTED_MARKERS = ["meridian university"]`, `BLOCKED_MARKERS = [... "university of maryland", "terrapin", "fafsa", "umd", "fdu" ...]`, enforced inside `build_vector_store`. A non-Meridian corpus **fails validation and the build returns None** | `rag_legacy.py:89-95`, `:226-229` |
| **B-2** | **Requires an external service in a third repository** (`enterprise-rag-core`, `:8010`) | `README.md:1-8`, `start_services.ps1:551-564` |
| **B-3** | **University vocabulary in prompts and user-facing strings** — `SYSTEM_PROMPT` names Meridian University; refusal text is *"I don't have that specific information in the university profile."*; `"Student's question:"` | `rag_legacy.py:116-136`, `rag.py:293,327,376` |
| **B-4** | **Import-time process-global env mutation** — sets `LANGCHAIN_*`/`HF_*` on import | `rag_legacy.py:34-41` |
| **B-5** | **Collection name is `"langchain"` by default** — LangChain's own default; read from `os.environ` **at import time**, not via config | `rag_legacy.py:53` |
| **B-6** | **Process-global singletons with no tenant key.** `_bm25_cache` is keyed by the literal `"bm25"` regardless of which collection is loaded, so a blue/green collection switch serves a **stale corpus index** | `rag_legacy.py:140`, `:359`, `:433` |
| **B-7** | **`app/mcp/` naming trap** — the directory that looks like the RAG server is the CRM server | `app/mcp/tools.py:19-86` |

## B.2 Corpus reality — structurally different from ANITA's

| | universityDemo | ANITA |
|---|---|---|
| Source | **one markdown file**, 15,608 B, 317 lines, 57 `##` sections | **32 HTML pages** scraped |
| Chunking | `RecursiveCharacterTextSplitter` on `\n### `/`\n\n` | per-section semantic chunks |
| Vectors | **35** | **141** |
| Metadata | `section`, `source_file`, `doc_type`, `ingested_at` | `product_id`, `chunk_type`, `source_url`, `category` |
| Build | offline only (`scripts/rebuild_rag_index.py`) | per-process, in memory |
| Retrieval | MMR `k`/`fetch_k`/`lambda_mult=0.5`; optional hybrid via `RAG_SEARCH_MODE=hybrid` using a stdlib `_MiniBM25` fused with **RRF k=60** | dense + `MiniBM25` + **weighted RRF k=60** |

**Note:** universityDemo *already has* RRF k=60 and a `_MiniBM25` in its hybrid path
(`rag_legacy.py:410-459`). ANITA's fusion is a refinement of the same idea, not a different one.

## B.3 What is genuinely worth adopting

| Component | Value | Evidence |
|---|---|---|
| **Chroma persistence** | Kills the ~25s per-process re-embed; the index survives restart | `rag_legacy.py:172-181`, `:263-289` |
| **Circuit breaker + fallback rungs** | `closed`/`half_open`/`open`; rungs recorded as `mcp\|local\|none`. Fault isolation ANITA has **none** of | `rag_mcp.py:348-383`, `rag.py:194-216` |
| **Facade + internal module split** | `rag.py` facade over `rag_legacy.py`, so callers import one name | `rag.py:34-63` |
| **Probe single-flight + abandoned-probe recovery** | Prevents one hung path from stopping recovery forever | `rag_mcp.py:424-463` |

### Breaker constants (cited, for the LLD)

```
threshold   : NONE — a single failure opens the circuit      rag_mcp.py:369-376
cooldown    : RAG_MCP_COOLDOWN, default 30s                  config.py:406-408
modes       : off | on | auto (default)                      rag_mcp.py:500-510
probe read  : max(read * RAG_MCP_PROBE_FRACTION, 0.5)        rag_mcp.py:56-58
              default 6.0 * 0.25 = 1.5s
fallback    : budget RAG_RETRIEVAL_BUDGET (8.0) minus        rag_mcp.py:68-70
              reserve RAG_FALLBACK_RESERVE (1.5)
resets on   : successful initialize or tools/call only       rag_mcp.py:185, :222
```

## B.4 Verified defects and hazards in the source

| # | Finding | Citation |
|---|---|---|
| B-8 | **A single failure opens the breaker** — no failure threshold. One transient timeout disables MCP for 30s | `rag_mcp.py:369-376` |
| B-9 | **Mode string normalisation is inconsistent**: `rag.py:67` uses `.lower()`, `admit_primary` uses `.strip().lower()`. A padded env value yields different modes in the two readers | `rag.py:67` vs `rag_mcp.py:500` |
| B-10 | An unrecognised mode is **admitted like auto but skips the fallback**, returning `""` with rung `"none"` | `rag.py:208`, `:215-216` |
| B-11 | **The MCP SDK is broken on this machine**: `mcp==2.0.0` ships no `mcp.server.fastmcp`, so `app/mcp/server.py` **always** serves its REST fallback; its `initialize` hardcodes protocol `"2024-11-05"` while the RAG client uses `"2025-06-18"` — two MCP surfaces, two protocol versions | `mcp/server.py:24-31`, `:114` vs `rag_mcp.py:39` |
| B-12 | `release_probe`, `mcp_available`, `mcp_retrieve_context` have **no callers** in-repo | `rag_mcp.py:466`, `:472`, `:252` |

## B.5 Verdict — RAG

**"As is" is not possible.** `build_vector_store` would **reject ANITA's corpus by design**
(B-1): ANITA has 32 HTML pages and zero "meridian university" markers. The build returns `None`
and there is no index at all.

So the real options are:

- **(a) Adopt the *architecture*, write ANITA-native internals** — facade + breaker + rungs +
  Chroma persistence, with the corpus gate and prompts written for ANITA. Satisfies CR-01.1,
  fails a literal reading of "same code".
- **(b) Adopt the code and lift the corpus gate + retarget vocabulary** — closest to "same code",
  requires editing `EXPECTED_MARKERS`/`BLOCKED_MARKERS`, `SYSTEM_PROMPT`, the refusal string, and
  the collection name. Keeps RRF/BM25/MMR structure.
- **(c) Adopt MCP-first including the external `enterprise-rag-core` service** — architectural
  parity in full, but adds a **third repository** and a network hop to a 141-chunk corpus.

**Q8 (new, for the user):** (a), (b) or (c)? Recommendation: **(b)** — it is the closest honest
reading of "same architecture and code", and B-1 is a corpus-validation constant rather than a
design decision. (c) should be decided separately and only if you intend to run ERC.
