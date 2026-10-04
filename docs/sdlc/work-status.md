> **Lens:** TPO · **Updated:** 2026-10-04 · **Scope:** every workstream — the SDLC plan (Stages 0–11), the D4 build (US-001…US-030), CR-01 (US-101…US-115), CR-02, and the open items.
> **Companion to:** `plan-state.md` (plan checkpoint) and `change-requests/STATUS.md` (CR-01 build checkpoint). This file is the **whole-of-work** view across all of them.

# Work Status — ANITA MEDICAL SYSTEMS

## 0. How to read this — and how the statuses were derived

**No story file records its own status.** All 30 files in `stories/` were checked: there is no
`Status:` field, no YAML frontmatter, no `## Status` heading, no checked `- [x]` box. Every
occurrence of the word "status" in that directory is code-level (a DB column, an enum value).

The statuses below are therefore **derived from build evidence**, in this order of precedence:

1. The module exists in `platform/app/anita/` and its test file passes.
2. The **route is actually served** — a module that exists but has no route is not `COMPLETED`.
3. The deferrals declared in `config/anita.yaml` → `release.deferred_use_cases`, with their reasons.
4. `scripts/system_check.py` and `scripts/release_gate.py` output.

Where code is migrated but not wired, this file says so rather than recording it as done. That
distinction is the whole point of the exercise: the plan-level scorecard (`08-coverage-verification.md`)
is at 100 %, and it explicitly states it "measures *plan* completeness, not product readiness".

### Status legend

| Status | Means |
|---|---|
| `COMPLETED` | Built, tested, and reachable in the running product |
| `IN PROGRESS` | Partly built — code exists but a route, worker or config is missing |
| `BLOCKED` | Cannot proceed until a named external thing exists (a decision, a provisioned number, a legal review) |
| `YET TO START` | No work begun; not blocked, simply not reached |
| `NOT FEASIBLE` | Analysed and judged not doable |

## 1. Summary

| Status | Count | Items |
|---|---|---|
| `COMPLETED` | **32** | 22 of US-001…US-030 · 10 of US-101…US-115 · Stages 0–11 |
| `IN PROGRESS` | **7** | US-002, US-003, US-008, US-016, US-017, US-018, US-021 |
| `BLOCKED` | **2** | US-001 (needs a Twilio number) · CR-02 (needs decisions Q9–Q12) |
| `YET TO START` | **5** | DG-06 operational config · DG-08 legal review · U-1 · U-2 · U-3 (all in §6) |
| `NOT FEASIBLE` | **0** | Nothing in the plan is infeasible — the feasibility verdict is **Feasible** |

Stories: 22 + 7 + 1 = 30. `NOT FEASIBLE` is genuinely zero: `FEASIBILITY-STUDY.md` §1 returns
"Feasible. Recommended, with two blocking conditions before public launch."

---

## 2. SDLC plan — Stages 0–11

| Stage | Status | Comment |
|---|---|---|
| 0 Engagement | `COMPLETED` | Brownfield: source `D:\project\universityDemo`, 60,302 LOC / 170 files |
| 1 Intent | `COMPLETED` | `00-product-intent.md` — AS-01…AS-10 |
| 2 Requirements | `COMPLETED` | `01-brd.md` — BRD-01…BRD-23, Discovery Matrix 13/13 |
| 3 Use cases | `COMPLETED` | `02-use-cases-workflows.md` — UC-01…UC-15, 14/14 scenarios each; WF-01…WF-04 |
| 4 Data & state | `COMPLETED` | `03-data-state-analysis.md` — DAT-01…16, DG-01…13, SM-01…06 |
| 5 Coverage gate | `COMPLETED` | `04-coverage-gap-analysis.md` — COMPLETE, 3 iterations, CV-01…CV-03 |
| 6 Modularization | `COMPLETED` | `05-modularization.md` — MOD-01…MOD-10 |
| 7 Architecture | `COMPLETED` | `06-architecture.md` — per-module flows + scale/bottleneck table |
| 7b Reconciliation | `COMPLETED` | `07-brownfield-reconciliation.md` — REC-01…REC-23, file-by-file reuse |
| 8 Module docs | `COMPLETED` | `modules/` — TRD-01…TRD-48 |
| 9 Stories | `COMPLETED` | `stories/` — US-001…US-030, self-contained with HLD/LLD |
| 10 Mapping | `COMPLETED` | `08-coverage-verification.md` — 19/19 capabilities, gates G1–G10 |
| 11 Verification | `COMPLETED` | Scorecard Overall **PASS**, every category 100 % |

**Qualification on Stage 11:** the scorecard measures **plan** completeness. Its own §3 says so
directly — "This scorecard measures *plan* completeness, not product readiness." A `PASS` there is
not a claim that the product is launch-ready; §6 of this file covers what is actually still open.

---

## 3. Build — US-001…US-030

### MOD-01 Channel Gateway

| Story | Status | Comment |
|---|---|---|
| US-001 PSTN voice end to end over Twilio Media Streams | `BLOCKED` | **No voice route is served at all.** The engine has never carried a call — no carrier, no media stream, no barge-in under load. Deferred until a Twilio number is provisioned, because local STT/TTS round trips do not validate the path |
| US-002 Web chat, WhatsApp and a configurable extra channel | `IN PROGRESS` | Web chat works. The WhatsApp adapter **is registered** in the channel gateway, but no webhook route is served, so an inbound message has nowhere to land. Deferred until the text + lead product is validated with real buyers over web chat |
| US-003 Browser-mic behind a hardened public edge | `IN PROGRESS` | **Hardening is built** — `validate_twilio_signature()` and `RateLimiter` in `channels.py`. The admission-ticket route **is** served (`Route.VOICE_TICKET`), but there is no audio path behind it: a ticket is issued and nothing consumes the audio |

### MOD-02 Conversation Orchestrator

| Story | Status | Comment |
|---|---|---|
| US-004 Cross-channel conversation identity | `COMPLETED` | `orchestrator.py`; recognised across channels, thread stays coherent |
| US-005 Question classification and routing | `COMPLETED` | Commercial → SQL, catalogue → retrieval, clinical refused |
| US-006 Turn serialisation and transcript persistence | `COMPLETED` | Every turn ordered, persisted, reviewable |

### MOD-03 Retrieval & Grounding

| Story | Status | Comment |
|---|---|---|
| US-010 ANITA-only index build and validate | `COMPLETED` | `index.py` — promoted only on passing validation, atomic promotion (`SM-05`) |
| US-011 Hybrid retrieval with a grounding gate | `COMPLETED` | `retrieval.py` — `MiniBM25`, weighted RRF 0.65/0.35, `GroundingGate` |
| US-012 Provenance and honest absence | `COMPLETED` | `corpus.py` — read-only SQLite, explicit absence, caveat guard |

### MOD-04 Inference & Answer Composition

| Story | Status | Comment |
|---|---|---|
| US-013 Deterministic price / MOQ / delivery with caveat | `COMPLETED` | `answers.py` — fact registry, `ProductResolver` (Resolved/Ambiguous/NotFound), structural caveat enforcement |
| US-014 Grounded prose, clause-streamed for voice | `COMPLETED` | `llm.py` + `prompts.py` — Ollama/MLX backend, `ClauseCutter`, numeric-grounding verification |
| US-015 Keep selling when the model is down | `COMPLETED` | Circuit breaker; SQL answers survive, prose declines with a handoff |

### MOD-05 Speech Services

| Story | Status | Comment |
|---|---|---|
| US-016 STT: 20 ms frames, VAD, 600 ms floor | `IN PROGRESS` | `speech.py` migrated; **no live route** — same media gap as US-001 |
| US-017 Clause TTS with immediate barge-in stop | `IN PROGRESS` | Same media gap |
| US-018 VRAM admission, degrade voice before text | `IN PROGRESS` | `voice_admission.py` migrated; same media gap |

### MOD-06 Lead & Mock CRM

| Story | Status | Comment |
|---|---|---|
| US-019 Lead capture and dedupe | `COMPLETED` | `leads.py` |
| US-020 Mock CRM behind a swappable interface | `COMPLETED` | `crm.py` — `CrmStore` Protocol |
| US-021 Outbound queue and dialling worker | `IN PROGRESS` | `OutboundQueue` **is built** — window and attempt limit checked at claim time, `SELECT … FOR UPDATE SKIP LOCKED` semantics, abandoned-row reaping. **No worker dials.** Held deliberately: placing calls to real buyers needs the DG-08 compliance review to close first |
| US-022 Handoff records and rep notification | `COMPLETED` | Real `Handoff` opened against the CRM (added by US-114) |

### MOD-07 Sentiment & Escalation

| Story | Status | Comment |
|---|---|---|
| US-023 Per-turn sentiment scoring | `COMPLETED` | Rule-based fallback always works — preserved through the CR-01 merge |
| US-024 Composite, category and same-turn escalation | `COMPLETED` | Escalation reaches a real sink; idempotent per lead |

### MOD-08 Observability & Evaluation

| Story | Status | Comment |
|---|---|---|
| US-025 Dependency-free per-turn trace | `COMPLETED` | `observability.py` — stages, timings, retrieval outcome |
| US-026 Admin console read API | `COMPLETED` | Health, conversation review, honest absence |
| US-027 Frozen eval set and release quality gate | `COMPLETED` | 70 cases, all passing |

### MOD-09 Configuration, Isolation & Boot

| Story | Status | Comment |
|---|---|---|
| US-028 Typed config, boot refuses unread keys | `COMPLETED` | Catches keys set but never read; typed parsers never fall back silently |
| US-029 Isolation guard, refuse to boot | `COMPLETED` | Verified live: a copy-pasted sibling config (`ANITA_DB_NAME=admissions`, ports 8000/8501) is refused with exit code 2, naming the resource, both values and the fix |
| US-030 Platform detection, secrets, preflight | `COMPLETED` | Secrets masked in every render; a refused boot leaves no artifact behind |

### MOD-10 Client Surfaces

| Story | Status | Comment |
|---|---|---|
| US-007 Responsive web chat surface | `COMPLETED` | 50/50 browser scenarios pass, desktop **and** mobile |
| US-008 Browser voice client, typing one tap away | `IN PROGRESS` | Same media gap as US-003 — the ticket route is served, the audio path is not |
| US-009 Admin console behind a login | `COMPLETED` | Bearer-token admin surfaces (UC-12, UC-13, UC-15) |

---

## 4. CR-01 — RAG + Sentiment re-architecture

**10 / 10 stories `COMPLETED`. Integration checkpoints C1–C9 all `PASS`.**
Full per-story evidence lives in `change-requests/STATUS.md`; this is the roll-up.

| Workstream | Stories | Status | Headline evidence |
|---|---|---|---|
| RAG | US-101 shared `rag` package extraction | `COMPLETED` | 48 tests; zero `anita.*`/`app.*` imports (AST + `sys.modules` scan); zero `os.environ` writes on import |
| RAG | US-102 Configurable corpus gate | `COMPLETED` | ANITA corpus passes its own gate 141/141; a gate failure makes **0** embedder calls |
| RAG | US-103 Chroma persistence | `COMPLETED` | Cold build 1.1 s → warm retrieve **0.023 s, exactly 1 embed call — 47× faster** |
| RAG | US-104 Circuit breaker + rungs | `COMPLETED` | 20-thread barrier race → exactly 1 probe claim; mode truth table over 5 pairs |
| RAG | US-105 ANITA adapter **(live wiring)** | `COMPLETED` | Duck-type compatible; all 7 call sites unchanged; `ANITA_RAG_BACKEND=memory` rollback proven byte-identical |
| Sentiment | US-111 Shared sentiment core extraction | `COMPLETED` | `anita/sentiment.py` is now a facade; all 16 public names verified; `test_mod07` 51 tests unaffected |
| Sentiment | US-112 Sentiment config actually read | `COMPLETED` | Threshold collapsed from four literals to one constant, still operator-tunable (CV-12) |
| Sentiment | US-113 Fix unreachable Hot category | `COMPLETED` | **Fixed, not ported.** Measured boundary recorded: rule-based "ready to order" scores 0.290 against a 0.3 bound — 0.01 short |
| Sentiment | US-114 Escalation with a real sink | `COMPLETED` | **UC-10 `DEFERRED` → `PASS`**; two wiring defects fixed |
| Sentiment | US-115 Sentiment store Protocol | `COMPLETED` | Injected, never global; two cores proven independent |

**New code introduced:** `shared/rag` (8 files) and `shared/sentiment` (6 files) — product-agnostic,
neither imports `anita.*`. ANITA consumes both through `anita/rag_adapter.py` and the sentiment facade.

---

## 5. CR-02 — Production infrastructure (Postgres + Docker)

**Status: `BLOCKED` on decisions Q9–Q12.** Not started. Deliberately sequenced *after* CR-01.

| # | Decision needed | Recommendation on record |
|---|---|---|
| D-1 | PostgreSQL or MySQL? | **PostgreSQL** — `psycopg2-binary` is already a declared dependency (`requirements.txt:20`) and the sibling product already runs it. MySQL would add a second engine to the estate for no gain |
| D-2 | One server or two? | **Two logical databases, one server** — corpus is read-only and rebuilt by the scraper; CRM is read-write and transactional. Different lifecycles, backup policies and roles |
| D-3 | Does the scraper write directly, or does a loader import? | **A loader imports.** Coupling a batch job to a live server database means a rebuild needs production credentials and can half-fail against production |
| D-4 | Where does the loader live? | Not yet stated in the CR |

**Ordering constraint that must not be violated:** CR-02 changes the persistence layer *underneath*
CR-01's modules. Doing both at once makes every regression ambiguous — you cannot tell whether a
failure came from the store swap or the module change. **CR-01 first, against SQLite, then CR-02.**

**Store integration matrix** — the user's "full integration of all stores" requirement:

| Store | Today | After CR-01 | After CR-02 | Status |
|---|---|---|---|---|
| Corpus `data/out/ams.sqlite` | SQLite, read-only, 10 tables | unchanged | → PostgreSQL (loader import) | `IN PROGRESS` (CR-01 done, CR-02 not started) |
| CRM `logs/anita_crm.sqlite` | SQLite via `MockCrmStore` | unchanged; now the escalation sink | → `PostgresCrmStore` behind the existing `CrmStore` Protocol | `IN PROGRESS` |
| Vector `InMemoryStore` | ~25 s rebuild per process | → **Chroma, persisted** (US-103) | unchanged | `COMPLETED` |
| Sentiment in-memory dict | Lost on restart | → behind `SentimentStore` Protocol (US-115) | → Postgres adapter | `IN PROGRESS` |

---

## 6. Open items

| Item | Status | Comment |
|---|---|---|
| **DG-06** Auth, rate limiting, Twilio signature validation | `YET TO START` (ops) — code `COMPLETED` | **Correction to the standing record.** `validate_twilio_signature()`, `RateLimiter` and admin bearer auth are **built and tested**; rate limiting is on by default in the gateway. What remains is configuration and operations: `TWILIO_AUTH_TOKEN` is unset, so the release gate correctly reports "degraded — inbound webhooks cannot be signature-verified". This is now a **deployment task, not a build task** |
| **DG-08** Legal review of clinical-refusal wording | `YET TO START` | A human/legal task, not engineering. Blocks public launch, and blocks UC-09 outbound (calling real buyers) |
| U-1 UC-10 A1 "buyer declines a human" | `YET TO START` | Deferred — needs a decline intent that does not exist in the evidence. `escalation_enabled` is the operator equivalent for now |
| U-2 `MIN_LABELED_OUTCOMES` operator-tunable? | `YET TO START` | Currently a code default of 100 |
| U-3 Side A's exact BANT completeness rule | `YET TO START` | Not present in the evidence |

### Deferred use cases — by decision, not by defect

Each carries a written reason in `config/anita.yaml` → `release.deferred_use_cases`:

| UC | Reason (abbreviated) |
|---|---|
| UC-02 WhatsApp inbound | Adapter registered, no webhook route. Until the text + lead product is validated with real buyers over web chat |
| UC-03 Inbound voice call | No voice route at all; no carrier, no media stream, no barge-in under load. Until a Twilio number is provisioned |
| UC-04 Browser-mic question | Ticket route served, no audio path behind it. Deferred with UC-03 — the same media path serves both |
| UC-09 Outbound follow-up | `OutboundQueue` has no worker. Deliberate: calling real buyers needs DG-08 to close first |
| UC-14 Additional messaging channel | An extension point rather than a trigger — verified by design review and the adapter interface, not by a driven test |

---

## 7. Verification — run on this machine, 2026-10-04

| Check | Command | Result |
|---|---|---|
| Unit suite | `.venv/Scripts/python -m pytest tests -q` | **742 passed**, 1 skipped, 0 failed |
| Browser suite | `bash tests/e2e/run.sh` | **50 passed** (25 scenarios × desktop + mobile) in 1.8 m |
| System integrity | `scripts/system_check.py` | **16 passed · 0 failed · 5 deferred** — 37 modules loaded, 11 dependencies wired |
| Release gate | `scripts/release_gate.py` | **FAIL — false alarm.** See below |

### The release gate failure is an artifact, not a regression

`release_gate.py` reports `1 scenario(s) failed: a greeting is greeted`. The cause:

1. **Nothing was listening on `127.0.0.1:8085`.** The gate runs `npx playwright test` itself but
   **never starts an instance** — `tests/e2e/run.sh` does that, and the gate does not use it.
2. The spec file is `test.describe.configure({ mode: "serial" })`, so the first test's
   `ERR_CONNECTION_REFUSED` **skipped** the remaining 49 rather than failing them. Skipped ≠ failed,
   which is why it surfaced as exactly one failure in 4 seconds.

With an instance running, the same suite is **50/50 green**. The gate still has a real defect worth
fixing: it cannot distinguish *"the app isn't running"* from *"the product is broken"*, and reports
the former as the latter — precisely the confusion its own docstring says it exists to prevent.

---

## 8. Known documentation defects

| # | Defect | Where |
|---|---|---|
| 1 | The migration table lists MOD-01/02, MOD-05, MOD-06/07 and MOD-08/10 as `planned` when all are built, committed and tested | `plan-state.md` §Migration status |
| 2 | "Next action" still says **169 chunks**; reality is **141** (CV-04 resolved the plan as wrong, 169 was never propagated to this line) | `plan-state.md` §Next action |
| 3 | The gate cannot tell "no instance" from "product broken" | `scripts/release_gate.py` |

---

## 9. What is actually next

In priority order:

1. **Fix the release gate** (§8.3) — small, contained, and it currently produces a false `FAIL` on a
   suite that is genuinely green. A port preflight that reports *"no instance on :8085 — start
   `run.sh` first"* removes the ambiguity.
2. **Commit CR-01.** All ten stories' work — `shared/`, both adapters, three new test files — is
   **uncommitted** on branch `chore/secret-hygiene-and-config-audit`, a branch whose name describes
   unrelated work. It should be committed and moved to a branch that names CR-01.
3. **Answer CR-02 Q9–Q12** (§5) to unblock the production-infrastructure change request.
4. **DG-06 operational config** — set `TWILIO_AUTH_TOKEN` and confirm signature verification live.
5. **DG-08 legal review** — needs an owner; blocks public launch and UC-09.

Items 1–3 are engineering and can start immediately. Items 4–5 need decisions or people outside the
build.
