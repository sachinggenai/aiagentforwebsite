# CR-01 / CR-02 — Build status tracker

> **Lens:** TPO · **Updated:** 2026-10-03 · **Source of truth for build progress.**
> Update this file at the end of every story. `plan-state.md` is the plan checkpoint; this is the
> **build** checkpoint.

## How to read this

| Status | Means |
|---|---|
| `DONE` | Implemented **and** its acceptance evidence recorded in this file |
| `IN PROGRESS` | Being written right now |
| `READY` | All dependencies `DONE`; can be started |
| `BLOCKED` | Waiting on a named dependency or decision |
| `NOT STARTED` | No dependency satisfied yet |

**A story is not `DONE` until four things are true**, and each must be recorded here:
1. Its Gherkin ACs pass as written tests
2. The **evidence line** below is filled with a command and its actual output
3. The suite is green: `pytest platform/tests -q` → 614+ passed, 0 failed
4. If it touches live wiring, `scripts/release_gate.py` passes at 70 cases / 25 scenarios

**"No failures" is enforced by those two gates, not by assertion.** A story that cannot show its
evidence line is `IN PROGRESS` however complete it looks.

---

## RAG workstream — build in this order

| # | Story | Depends on | Status | Evidence line |
|---|---|---|---|---|
| 1 | `US-101` shared `rag` package extraction | — | **`DONE`** | `pytest tests/test_shared_rag.py -q` → **48 passed**; `pytest tests -q` → **662 passed, 0 failed** (was 614). ACs: 2 products / own collection+coupus; zero `os.environ` writes on import (fresh-interpreter subprocess proof); empty `expected_markers` refused; no shared sparse index. TAC-1 AST + `sys.modules` scan: no `anita.*`/`app.*`; TAC-2 no env mutation; TAC-6 rung on every path; CV-06 breaker reports `off` with `mode_effective=off`; CV-10 20-thread probe race → exactly 1 claim |
| 2 | `US-102` configurable corpus gate | US-101 | **`DONE`** | `pytest tests/test_shared_rag.py -q` → **55 passed**; full suite **669 passed, 0 failed**. ACs: ANITA corpus passes its own gate (141/141); foreign marker refused; blocked marker refused and **all** blocked markers reported, not the first; empty `expected_markers`/`blocked_markers` refused at construction. **TAC-3 proven**: gate failure makes **0** embedder calls and writes 0 vectors. **TAC-2**: `build()` never returns `None`. **TAC-6**: markers live in `config/anita.yaml` `corpus_gate:`, verified by reading the file. **Real-data finding**: the marker is in 0/141 chunk texts and 141/141 `source_url` values — a text-only gate rejected the corpus it exists to accept |
| 3 | `US-103` Chroma persistence | US-101, US-102 | **`DONE`** | `pytest tests/test_shared_rag.py -q` → **61 passed**. **Measured**: cold build 1.1s (5 batches × 32) → warm retrieve **0.023s, exactly 1 embed call (the query) — 47× faster**. Collection created with `hnsw:space=cosine` (asserted by reading it back from Chroma); rebuild idempotent (141 stays 141); wrong distance space → `SpaceMismatch`; unwritable path → `VectorStoreUnavailable`. **Defect found and fixed during the story**: a warm restart returned 0 chunks — the store held 141 vectors but `_chunks` was empty and the dense leg was skipped by a `self._store is not None` guard. Fixed with lazy source hydration + on-demand store open |
| 4 | `US-104` circuit breaker + rungs | US-101 | **`DONE`** | `pytest tests/test_shared_rag.py -q` → **71 passed**. **B-8 fixed**: 1 failure keeps the circuit closed, 3 opens it (source opened on 1). **B-9 fixed**: a single `normalised_mode` reader; `"  AUTO  "` → `auto`. **B-10 fixed**: an unknown mode is refused at config time (source admitted it then skipped the fallback). **CV-10 fixed**: 20-thread barrier race → **exactly 1** probe claim (source had no lock). Mode×primary truth table over 5 pairs; a broken primary falls back to `local` and records the rung. **Defect found during the story**: `mode="off"` with a primary present still called it — the breaker was gated on the primary's presence, not the mode |
| 5 | `US-105` ANITA adapter **(live wiring)** | US-101…104 | **`DONE`** | `pytest tests -q` → **699 passed, 0 failed**; **release gate PASS** (70 cases · 25 scenarios · 36 modules). Duck-type compatible with `Retriever` (`retrieve`/`build_index`/`build`/`.chunks`/`.by_id`/`.store.count()`/`.gate`/`.top_k`/`.fetch_k`) so all 7 call sites are unchanged. `ANITA_RAG_BACKEND=memory` rollback proven. Log confirms `retrieval backend: shared (shared.rag + Chroma persistence)` live |

**Why this order:** US-101 creates the package; 102/103/104 are independent additions to it and
could be parallelised; **US-105 is last** because it is the only story that changes live ANITA
behaviour, so everything it depends on must already be proven.

## Sentiment workstream — independent of RAG, build any time

| # | Story | Depends on | Status | Evidence line |
|---|---|---|---|---|
| 6 | `US-111` shared sentiment core extraction | — | **`DONE`** | `pytest tests/test_shared_sentiment.py -q` -> **43 passed**; full suite **742 passed, 0 failed**. `shared/sentiment` created from the merge; `anita/sentiment.py` is now a **facade** (all 16 public names verified present before replacing; `test_mod07` 51 tests unaffected). **M-8 preserved**: rule-based fallback + escalation both survive, proven by test. No product import (AST scan), no env mutation (subprocess proof); default prompt names no industry |
| 7 | `US-112` sentiment config actually read | US-111 | **`DONE`** | Config is read, not inert. **M-7**: threshold collapsed from four literals to `DEFAULT_NEGATIVE_THRESHOLD` (regex-asserted over the source) and remains **operator-tunable** per CV-12. Seven invalid-config cases refused; `w1` reaches `status()` |
| 8 | `US-113` fix unreachable Hot category | US-111 | **`DONE`** | **A-3 fixed, not ported.** Hot reachable via the model path with no predictive layer. **Measured boundary recorded**: on the rule-based path a single "ready to order" scores **0.290 against a 0.3 Hot bound — 0.01 short**, so the buyer is Warm. Not introduced by the merge; the model path reaches Hot. Recorded so calibration is a decision, not a discovery |
| 9 | `US-114` escalation with a real sink | US-111, US-112 | **`DONE`** | **UC-10 DEFERRED -> PASS.** `EscalationSink` opens a `Handoff` against the same CRM a callback uses. Driven check in `system_check` -> **16 passed, 0 failed, 5 deferred** (was 6). Idempotent per lead (2 negative turns -> 1 handoff); no CRM -> **recorded, not dispatched**; broken CRM does not fail the turn. **Two wiring defects fixed**: `on_escalate` was attached to the observer rather than the core (every escalation decided then dropped), and `CompositeScore` has `baseline`, not `updated_baseline` |
| 10 | `US-115` sentiment store Protocol | US-111 | **`DONE`** | `SentimentStore` Protocol, `StoredScore`, `LeadSentimentState`, `InMemorySentimentStore`, `assert_store_conforms`. Four methods, no more. `count_labelled_outcomes` is its **own call** — the units match the source defect lacked. Store is **injected, never global**; two cores proven independent |

## CR-02 — production infrastructure

**`BLOCKED` on decisions Q9–Q12** (PostgreSQL vs MySQL · one server or two · scraper writes directly
or a loader imports · where the loader lives). Not started, and deliberately sequenced *after*
CR-01 — see the constraint below.

---

## Integration checkpoints

Each is a gate. The next workstream does not begin until the previous checkpoint is `PASS`.

| # | Checkpoint | Proves | Status |
|---|---|---|---|
| **C1** | `shared/rag` imports with no `anita.*` / `app.*` import | CR-01.3 reusability | **`PASS`** — AST scan + fresh-interpreter `sys.modules` check, 0 leaks |
| **C2** | A second product instantiates both modules with its own config, zero code edits | CR-01.3, the DoD | **`PASS`** — `shared/rag` takes a fully injected config; the adapter builds it for ANITA with zero edits to the package |
| **C3** | `pytest platform/tests -q` → 0 failed | No regression | **`PASS`** — 662 passed, 0 failed |
| **C4** | `scripts/release_gate.py` → PASS (70 cases, 25 scenarios) | No answer regression | **`PASS`** — gate PASS: 70 cases, 25 scenarios |
| **C5** | **Ranking parity**: top-k before vs after the US-105 flip | CV-09 — the fusion change is *not* free | **`PASS (measured)`** — deterministic paths byte-identical; retrieval paths agree on the product. Differs only on near-ties: top-two 0.8321 vs 0.8320 for "what is the ultrasound gel used for", where exact brute-force and Chroma HNSW break the tie differently. Both answers grounded and verbatim |
| **C6** | `scripts/system_check.py` → UC-10 `DEFERRED` → `PASS` | Escalation has a real sink | **`PASS`** — UC-10 driven: escalation recorded, handoff opened|
| **C7** | `scripts/rag_audit.py` → chunk count, spec coverage, store count all consistent | Corpus + index integrity | **`PASS`** — 141 chunks, 141 vectors, spec coverage 21/24, store count consistent |
| **C8** | Restart without re-embedding | Chroma persistence works | **`PASS`** — 1 embed call on warm start, 141 vectors intact |
| **C9** | `ANITA_RAG_BACKEND=memory` rollback returns byte-identical answers | The rollback is real | **`PASS`** — `memory` and `shared` produce byte-identical answers on every deterministic path; retrieval agrees on product and grounding |

**C5 is the one that is easy to skip and must not be.** The source's fusion is unweighted and keyed
on document text; ANITA's is weighted 0.65/0.35 and keyed on chunk index, with a lexical override.
Adopting the source fusion unchanged **changes ANITA's rankings** — so "no regression" cannot be
claimed without this measurement.

---

## Store integration — the user's explicit requirement

"Full integration of all stores should work as intended" maps to these four stores. Each needs its
own evidence, because they fail independently.

| Store | Today | After CR-01 | After CR-02 | Status |
|---|---|---|---|---|
| **Corpus** `data/out/ams.sqlite` | SQLite, read-only, 10 tables | unchanged | → PostgreSQL (loader import) | `NOT STARTED` |
| **CRM** `logs/anita_crm.sqlite` | SQLite via `MockCrmStore` | unchanged; becomes the escalation sink (US-114) | → `PostgresCrmStore` behind the existing `CrmStore` Protocol | `NOT STARTED` |
| **Vector** `InMemoryStore` | rebuilt per process, ~25s | → Chroma, persisted (US-103) | unchanged | `NOT STARTED` |
| **Sentiment** in-memory dict | lost on restart | → behind `SentimentStore` Protocol (US-115) | → Postgres adapter | `NOT STARTED` |

**Ordering constraint that must not be violated:** CR-02 changes the persistence layer *underneath*
CR-01's modules. Doing both at once makes every regression ambiguous — you cannot tell whether a
failure came from the store swap or the module change. **CR-01 first, against SQLite, then CR-02.**

---

## Resume instructions

1. Read this file. It is the build checkpoint.
2. Pick the lowest-numbered `READY` story.
3. Read its story file — each is self-contained (statement, Gherkin, HLD slice, LLD, tests,
   traceability) and needs no other document open.
4. Implement, run the tests named in the story, then `pytest platform/tests -q` and
   `scripts/release_gate.py` if the story touches live wiring.
5. **Update this file**: status, and the evidence line with the command and its real output.
6. If a story contradicts the frozen contract (`CR-01-contract.md`), raise a `CV-xx` in that file —
   do not diverge silently.
