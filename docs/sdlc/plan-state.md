> **Last updated:** 2026-10-03 · **Engagement:** Brownfield (source repo `D:\project\universityDemo`, 60,302 LOC / 170 files) → target `D:\project\anitamedicalsystems\platform`

# Plan State — ANITA MEDICAL SYSTEMS Product Marketing & Support Assistant

## Change requests

| CR | Title | Status | Artifacts |
|---|---|---|---|
| **CR-01** | Re-architect RAG + Sentiment to the universityDemo pattern, as standalone reusable modules | **PLANNED — ready to build** | `change-requests/CR-01-rag-sentiment-rearchitecture.md` (evidence, Appendices A/B), `CR-01-contract.md` (frozen interface, C1–C10), `CR-01-architecture.md`, `CR-01-modularization-delta.md`, `change-requests/stories/US-101…105`, `US-111…115` |

**CR-01 decisions (user-approved 2026-10-03):** Q7→(a) shared parameterised sentiment core with
escalation inside it; Q8→(b) adopt the RAG code, lift the corpus gate, retarget vocabulary. The
external `enterprise-rag-core` service is **out of scope**.

**CR-01 open items at hand-off:** 3 explicit Unknowns (C10.2: UC-10 A1 decline intent,
`MIN_LABELED_OUTCOMES` tunability, Side A's BANT rule) and 1 plan correction (CV-04: the frozen
figures below say 169 chunks; reality is 141). Neither blocks starting US-101.

**CR-01 contradiction register — CV-04…CV-12 (CV-01…CV-03 belong to the frozen baseline):**


**Build status tracker:** `change-requests/STATUS.md` — per-story status, integration checkpoints C1–C9, and the store-integration matrix. **This is the build checkpoint; update it after every story.**

**Build order:** US-101 → US-102 → US-103 → US-104 → US-105 (adapter last — the only story that
touches live ANITA behaviour), then US-111 … US-115.

## Confirmed decisions (user-provided, 2026-10-03)

| # | Decision |
|---|---|
| D1 | **Platforms:** runtime OS = Windows + macOS/Linux; **plus** additional messaging channels; **plus** web + mobile-web client surfaces; **plus** voice over PSTN *and* browser mic |
| D2 | **Target location:** `D:\project\anitamedicalsystems\platform\` — scraper and platform share one repo; the platform reads the corpus from `data/out/` |
| D3 | **Twilio:** add a **new number on the same Twilio account** — same credentials, separate webhook/number, no interference with the live university assistant |
| D4 | **Delivery:** full SDLC plan **+ actual code migration** (copy reusable modules, strip Salesforce, wire new config) |

## Stage status

| Stage | Status | Evidence |
|---|---|---|
| 0 Engagement | done | Stage 0 block in `00-product-intent.md` |
| 1 Intent | done | `00-product-intent.md` — AS-01…AS-10 |
| 2 Requirements | done | `01-brd.md` — BRD-01…BRD-23 (Discovery Matrix 13/13, Evidence Register) |
| 3 Use cases | done | `02-use-cases-workflows.md` — UC-01…UC-15 (14/14 scenarios each), WF-01…WF-04 |
| 4 Data & state | done | `03-data-state-analysis.md` — DAT-01…DAT-16, DG-01…DG-13, SM-01…SM-06 |
| 5 Coverage gate | done | `04-coverage-gap-analysis.md` — STATUS: COMPLETE, 3 iterations, CV-01…CV-03 |
| 6 Modularization | done | `05-modularization.md` — MOD-01…MOD-10 |
| 7 Architecture | done | `06-architecture.md` — per-module flows + scale/bottleneck table |
| 7b Reconciliation | done | `07-brownfield-reconciliation.md` — REC-01…REC-23 |
| 8 Module docs | done | `modules/` — MOD-01…MOD-10, defining TRD-01…TRD-48 |
| 9 Stories | done | `stories/` — US-001…US-030, all self-contained with HLD/LLD |
| 10 Mapping | done | `08-coverage-verification.md` — 19/19 capabilities, story→test mapping, gates G1–G10 |
| 11 Verification | done | Validator: **1086 passed / 0 failed** · Scorecard Overall: **PASS** · STATUS: **COMPLETE** |

## ID registry

AS-01…AS-10 · BRD-01…BRD-23 · UC-01…UC-15 · WF-01…WF-04 · DAT-01…DAT-16 · DG-01…DG-13 · SM-01…SM-06 · CV-01…CV-12 · MOD-01…MOD-10 · REC-01…REC-23 · TRD-01…TRD-48 · US-001…US-030 · US-101…US-105 · US-111…US-115

## TRD allocation (as built)

| Module | TRD range | Stories |
|---|---|---|
| MOD-01 Channel Gateway | TRD-01…TRD-06 | US-001…US-003 |
| MOD-02 Conversation Orchestrator | TRD-07…TRD-11 | US-004…US-006 |
| MOD-03 Retrieval & Grounding | TRD-12…TRD-16 | US-010…US-012 |
| MOD-04 Inference & Answer Composition | TRD-17…TRD-21 | US-013…US-015 |
| MOD-05 Speech Services | TRD-22…TRD-25 | US-016…US-018 |
| MOD-06 Lead & Mock CRM | TRD-26…TRD-31 | US-019…US-022 |
| MOD-07 Sentiment & Escalation | TRD-32…TRD-35 | US-023…US-024 |
| MOD-08 Observability & Evaluation | TRD-36…TRD-40 | US-025…US-027 |
| MOD-09 Configuration, Isolation & Boot | TRD-41…TRD-44 | US-028…US-030 |
| MOD-10 Client Surfaces | TRD-45…TRD-48 | US-007…US-009 |

## Frozen constants (agreed across all modules)

Database `anita_ams`, schema `anita` · Chroma collection `anita_ams__v1` (768-d, nomic-embed-text) · service ports 8100 / 8601 / 8602 · log directory `logs/anita` · forbidden sibling values: db `admissions`, collection `langchain` / `meridian_kb__v1`, ports 8000 / 8501 / 8502 / 8098 / 1234, log dir `logs`.

## Migration status (D4 — code migration into `platform/`)

| Module | Status | Evidence |
|---|---|---|
| **MOD-09** Configuration, Isolation & Boot | ✅ **done** | `platform/app/anita/{config,config_truth,isolation,platform_detect,secrets,boot,errors}.py` · 72 tests · preflight green · refusals demonstrated |
| **MOD-04** Deterministic answer path (US-013) | ✅ **done** | `platform/app/anita/answers.py` — fact registry, `ProductResolver` (Resolved/Ambiguous/NotFound), structural caveat enforcement |
| **MOD-03** Corpus access (TRD-12 groundwork) | ✅ **done** | `platform/app/anita/corpus.py` — read-only SQLite, explicit absence, caveat guard |
| **MOD-03** Retrieval & Grounding (BM25, weighted RRF, grounding gate) | ✅ **done** | `platform/app/anita/retrieval.py` — `MiniBM25`, `weighted_rrf`, `GroundingGate`, `Retriever`, `InMemoryStore` |
| **MOD-03** Embeddings, store & index lifecycle | ✅ **done** | `platform/app/anita/{embeddings,stores,index}.py` — Ollama adapter with task prefixes, Chroma store with collection guard, `SM-05` lifecycle with atomic promotion, corpus ingestion guard |
| **MOD-04** LLM path, prompt assembly & grounding check | ✅ **done** | `platform/app/anita/{llm,prompts}.py` — Ollama/MLX backend, `ClauseCutter` streaming, circuit breaker, numeric-grounding verification, caveat enforcement on output |
| MOD-01/02 Channel Gateway & Orchestrator | next | — |
| MOD-01 Channel Gateway · MOD-02 Orchestrator | planned | — |
| MOD-05 Speech Services | planned | — |
| MOD-06 Lead & Mock CRM · MOD-07 Sentiment | planned | — |
| MOD-08 Observability · MOD-10 Client Surfaces | planned | — |

**MOD-09 acceptance (US-028/029/030):** the isolation guard refuses a colliding boot with a machine-greppable message naming the resource, both values and the fix; the config audit catches keys set but never read; typed parsers name the offending key and never fall back silently; secrets are masked in every render; and a refused boot leaves no artifact behind — no port, no connection, no report file.

Verified live: a copy-pasted sibling config (`ANITA_DB_NAME=admissions`, ports 8000/8501, `ANITA_LOG_DIR=logs`) is refused with exit code 2.

## Next action

Continue the D4 migration in the planned order:

1. **MOD-03 + MOD-04** (US-010…US-015) — the answer engine over `../data/out` (169 chunks, 24 products). Buildable and validatable with no telephony, and it produces the frozen evaluation set that gates every later release.
2. **MOD-02 + MOD-01 text channels** (US-004…US-006, US-002) — orchestrator, identity, web chat and WhatsApp, plus the `DG-06` hardening those stories carry.
3. **MOD-05 voice**, then **MOD-06/07**, then **MOD-08/10**.

Two items remain **blocking for public launch** and must have an owner before go-live: `DG-06` (auth, rate limiting, Twilio webhook signature validation) and `DG-08` (legal review of clinical-refusal wording).
