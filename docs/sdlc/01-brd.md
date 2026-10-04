> **Lens:** BA · **Decided by:** BA, confirmed by PO · **Engagement:** Brownfield (`D:\project\universityDemo` → `D:\project\anitamedicalsystems\platform`) · **Inputs:** `00-product-intent.md`, Stage 0 code reconnaissance · **Defines:** BRD-01 … BRD-23

# BRD — ANITA MEDICAL SYSTEMS Product Marketing & Support Assistant

## 0. Discovery Matrix — first completeness gate

| Dimension | Questions | Evidence | Gaps |
|---|---|---|---|
| **Personas** | Who uses it? Who is served? | User-provided (procurement officer, radiographer, service engineer, sales rep, ops) + Code-observed (the existing system already serves caller/counsellor/operator personas) | Whether buyers will trust AI for commercial answers — **AS-04**, validate with 5 buyer interviews before Stage 6 |
| **Actors** | Who/what interacts with the system? | Code-observed: caller, WhatsApp user, web-chat user, outbound-call target, admin operator, background workers; new: browser-mic user, additional messaging-channel user | Which additional messaging channels — `DG-07` (decision deferred to Stage 4) |
| **Business goals** | Why does it exist? | User-provided: answer faster, capture more qualified leads, cover out-of-hours demand | Quantified baseline of current enquiry volume — **Unknown**; no analytics on the TradeIndia catalogue |
| **Capabilities** | What must the system do? | Derived from the four channels + corpus: answer catalogue questions, look up price/MOQ/delivery deterministically, qualify and capture leads, score sentiment, escalate to human, run outbound follow-up | Whether outbound calling is in MVP or phase 2 — user listed it, so **in scope**, but volume/cadence unspecified (`DG-04`) |
| **Workflows** | How does work move end-to-end? | Code-observed: the admissions platform already implements inbound-call, outbound-call, WhatsApp and web-chat workflows end to end (`app/main.py`, `app/outbound/caller.py`) | B2B sales handoff workflow (who receives a lead, how fast) — not documented by ANITA |
| **States** | What states can entities enter? | Code-observed: `leads.status`, `call_queue.status`, `offer_letters.status`, sentiment `category` all exist as enums | New lead lifecycle for B2B (no `offer_letters` equivalent) — `SM-01` |
| **Data** | What data is required? | Derived: the collected corpus is complete and validated (`data/out/`, 24 products, 36 FAQs, 169 chunks, 10 SQLite tables) | Vehicle/territory data for outbound prioritisation — `DG-05` |
| **Integrations** | What external systems exist? | Code-observed: Twilio (voice + WhatsApp), Ollama, MLX, Chroma, PostgreSQL, SMTP, Cloudflare tunnel, MCP retrieval | **Mock CRM replaces Salesforce** (`DG-02`); additional messaging channels need their own SDKs (`DG-07`); Twilio **new number** required (D3) |
| **Security** | What can go wrong? | Code-observed: **the source system has no auth on any route, no Twilio signature validation, no rate limiting** (`app/main.py` — every route public) | Must be fixed for a public commercial deployment — `DG-06`. Also `.env.bak-*` files with live secrets exist in the source repo |
| **Failure** | What happens when dependencies fail? | Code-observed: Postgres degrades gracefully (every helper returns `None`); RAG has a circuit breaker with fallback; LLM has no fallback | No LLM fallback path — `DG-03`; new: what does a buyer hear when the model is down? |
| **Scale** | What happens at 10×/100×? | Code-observed: source system is GPU-bound, 16,311 MiB ceiling, `MAX_CONCURRENT_CALLS` admission control; measured 600 ms endpointing floor | ANITA concurrency target unspecified — `DG-01`; corpus is 100× smaller than the university KB so retrieval is not the bottleneck |
| **Compliance** | What constraints exist? | Derived: medical-equipment claims are regulated; the corpus is a **product catalogue, not clinical guidance** | Clinical-question refusal policy wording needs legal review — `DG-08` |
| **Operations** | How is it monitored/recovered? | Code-observed: `perf_trace.py`, `/api/calls/live`, Streamlit cockpit, `logs/perf_turns.jsonl`; no CI/CD, no alerting | Per-product observability must be isolated (no shared logs) — `DG-09` |

## 1. Objectives

| # | Objective | Traces to metric |
|---|---|---|
| O-1 | Answer catalogue questions instantly, on any channel, at any hour | North Star (qualified leads/week) + first-response latency |
| O-2 | Never state a product fact that is not in the published catalogue | Hallucinated price/spec rate = 0; grounded-answer rate ≥ 98 % |
| O-3 | Convert conversations into qualified, contextualised leads | North Star |
| O-4 | Detect buyer frustration and route to a human before the lead is lost | Sentiment capture 100 %; negative handoff within 1 turn |
| O-5 | Run as a fully isolated instance from the university product | Cross-product isolation = zero shared state |

## 2. Scope

| In scope | Out of scope |
|---|---|
| Four+ channels on one answer engine (web chat, voice in, voice out, WhatsApp, browser mic, additional messaging) | Payments, order placement, e-commerce |
| RAG over the collected catalogue corpus | CRM replacement or pipeline management |
| Deterministic SQL lookups (price, MOQ, delivery, GST, address) | Quotation PDF generation and e-signature |
| Lead capture, qualification, mock-CRM persistence, human handoff | Real Salesforce or any third-party CRM integration |
| Sentiment scoring and negative-sentiment escalation | Clinical or diagnostic advice |
| Per-product isolated DB / vector index / model config / ports | Multi-tenant shared deployment |
| Windows + macOS/Linux runtime parity | Mobile native applications |

## 3. Stakeholders

| Stakeholder | Role | Responsibility | Interest |
|---|---|---|---|
| ANITA sales director | Business owner | Accepts the MVP boundary, receives leads | Lead volume and quality |
| ANITA sales reps (Arun) | Lead consumers | Follow up on handed-off leads | Context-rich, prioritised leads |
| ANITA IT / ops | Instance operator | Keep channels healthy, manage Twilio/GPU | Uptime, cost, no interference with the university system |
| Meridian platform owner | Source-code owner | Approves reuse boundaries | The live university assistant must not regress |
| BA / PO / TPO / Architect | Delivery | This plan | Traceable, buildable scope |

## 4. Business Requirements

### Channel & Access
### BRD-01 — Single answer engine across all channels
Every supported channel (web chat, PSTN voice inbound, PSTN voice outbound, WhatsApp, browser-mic voice, and any additional messaging channel added later) must obtain its answer from the same retrieval-and-answer engine, so a catalogue correction made once is reflected everywhere.

### BRD-02 — Channel-agnostic conversation continuity
A buyer who starts a conversation on one channel must be identifiable when they continue on another, using their phone number or a session identity, and the prior context must be available to the answer engine.

### BRD-03 — Twilio isolation on a separate number
The ANITA instance must use its own Twilio phone number and WhatsApp sender on the shared Twilio account, so inbound traffic for each product is routed independently and the university assistant is unaffected.

### Answer Quality
### BRD-04 — Grounded answers only
Every product fact stated to a buyer must be traceable to a specific record in the collected corpus. The system must refuse rather than improvise when retrieval returns nothing relevant.

### BRD-05 — Deterministic commercial lookups
Price, MOQ, delivery time, supply ability, GST number, PAN, and company address must be answered from structured storage (SQL), not from language-model generation, so the values cannot drift.

### BRD-06 — Price caveat is mandatory
Every price stated must be accompanied by the list-price / quotation-required caveat (`is_list_price = true`, `quote_required = true` in the corpus). A price stated without it is a defect.

### BRD-07 — Specification answering
The system must answer specification questions for all 24 catalogue products, including products whose specification block is empty on the source site (it must say so, not invent).

### BRD-08 — Company and contact answering
The system must answer questions about the company — statutory details, directors, certifications (ISO 9001:2015, NSIC-CRISIL), GST, addresses (Mumbai, Delhi), phone — from the collected company facts.

### BRD-09 — Same-named product disambiguation
Where two catalogue products share a name (**"Medical Dry Imager"** exists twice at ₹5,50,000 and ₹2,00,000), the system must disambiguate by product id or price before answering, and must never conflate them.

### BRD-10 — Clinical question refusal
The system must decline medical, diagnostic or clinical-advice questions and route them to a human, because the corpus is a product catalogue and not clinical guidance.

### Lead Lifecycle
### BRD-11 — Lead capture on every channel
Every conversation that yields a name, phone, email or expressed interest must create or update a lead record with the channel, the transcript and the products discussed.

### BRD-12 — Lead qualification
Leads must be qualified on the signals the corpus and conversation provide — product interest, quantity, urgency, geography, buyer type — and stored as structured fields, not free text.

### BRD-13 — Human handoff
The system must be able to hand a conversation to a human sales representative, producing a handoff record that carries the buyer's context, the products discussed, the sentiment and the reason for escalation.

### BRD-14 — Mock CRM seam
Lead and interaction data must be written through a CRM interface backed by a **local mock database**. No Salesforce code, schema, client or credential may be reused from the source repository, and the interface must be swappable for a real CRM later without changing callers.

### Sentiment & Escalation
### BRD-15 — Sentiment scoring on every conversation
Every conversation turn must be scored for sentiment and buying intent, producing a per-lead composite score and category.

### BRD-16 — Negative-sentiment escalation
When a conversation's sentiment crosses the configured negative threshold, the system must escalate to a human within the same turn, rather than continuing to answer automatically.

### BRD-17 — Objection capture
Objections and friction signals raised by a buyer must be captured as structured data on the lead, since they are the primary input to the sales follow-up.

### Operations
### BRD-18 — Instance isolation
The ANITA instance must have its own database, its own vector collection, its own model configuration, and its own service ports. It must share no row, no index, no model process and no log stream with the university deployment.

### BRD-19 — Cross-platform runtime
The stack must start and serve on Windows and macOS/Linux from the same configuration, selecting the appropriate model backend per platform.

### BRD-20 — Configuration is a single source of truth
Every configured value must be read by the code that acts on it. A key that is written but never read is a defect.

### BRD-21 — Per-conversation observability
Every conversation must emit a trace covering channel, turn count, retrieval hit/miss, answer latency, sentiment and outcome, isolated per product instance.

### BRD-22 — Quality gate on answer correctness
The system must carry a frozen evaluation set of catalogue questions with known correct answers, and no release may regress against it.

### BRD-23 — Outbound follow-up
The system must be able to place an outbound call or send an outbound message to a captured lead, respecting a per-lead attempt limit and an approved calling window.

## 5. Business Rules

| Rule | Condition → Action | Source |
|---|---|---|
| R-1 | Any price quoted → append list-price/quote-required caveat | Derived (`price_is_list_price`, `quote_required` in corpus) |
| R-2 | Retrieval returns no chunk above threshold → say so and offer a human handoff; never answer from model memory | PO, from AS-06 and the source system's RAG circuit-breaker pattern |
| R-3 | Two products share a name → require disambiguation before answering | Derived (corpus contains "Medical Dry Imager" × 2) |
| R-4 | Question is clinical/diagnostic → refuse, escalate, log | Compliance (`DG-08`) |
| R-5 | Sentiment crosses negative threshold → escalate within the turn | PO objective O-4 |
| R-6 | Outbound attempts for a lead ≥ configured maximum → stop and mark exhausted | Code-observed (`MAX_CALL_ATTEMPTS` in source) |
| R-7 | Source of an answer is unknown → do not answer | PO objective O-2 |

## 6. Evidence Register

| Fact | Value | Classification | Evidence |
|---|---|---|---|
| Corpus size | 24 products, 32 pages, 169 chunks, 36 verbatim FAQs, ~20.5 K tokens | Derived | `data/out/reports/validation.md` |
| Corpus has structured price/MOQ/delivery | `products` table, 24 rows, numeric columns indexed | Derived | `data/out/tables_schema.json`, `ams.sqlite` |
| Corpus records price as list price | `price_is_list_price=1`, `quote_required=1` | Derived | `data/out/tables/products.csv` |
| Duplicate product name | "Medical Dry Imager" × 2 (270683 ₹5,50,000; 3935251 ₹2,00,000) | Derived | `ams.sqlite` `products` |
| Source platform has no auth | Every FastAPI route unauthenticated; no `RequestValidator`; no CORS/rate limit | Code-observed | `app/main.py` route census; agent recon §6 |
| Source CRM is Salesforce-specific | `app/crm/` 3,653 LOC, httpx client to `CRM_BASE_URL` (`:8098`) | Code-observed | `app/crm/client.py` docstring |
| `app/crm/session.py` is not CRM-coupled | Pure conversation-identity registry, no network I/O | Code-observed | `app/crm/session.py` docstring |
| Sentiment already exists | `app/sentiment/` 1,239 LOC, LLM-extraction + weighted composite + categories | Code-observed | `app/sentiment/scorer.py` |
| Sentiment is LLM-based, not a classifier model | `SENTIMENT_EXTRACTION_PROMPT` → strict JSON via Ollama | Code-observed | `app/sentiment/scorer.py:93–126` |
| Voice endpointing floor | 600 ms end-of-speech detection | Code-observed | `doc/sdlc/00-product-intent.md` (measured) |
| Two divergent vector stores in source | local Chroma collection `langchain` (35 docs) + ERC `meridian_kb__v1` (1,144 docs) | Code-observed | agent recon §4 |
| Twilio integration surface | 7 files; Media Streams over WebSocket; no `<Gather input="speech">`, no recording | Code-observed | agent recon §1 |
| DB is PostgreSQL, no ORM | raw `psycopg2`; idempotent `CREATE TABLE IF NOT EXISTS` on every boot | Code-observed | `app/database.py:init_db()` |
| CRM migration runs unconditionally | `app/database.py` executes `ALL_CRM_SQL` regardless of `CRM_ENABLED` | Code-observed | `app/database.py` ~102–105 |
| Secrets on disk in source repo | `.env`, `.env.bak-pre-us006`, `.env.bak.20260919T041845` present, gitignored | Code-observed | repo listing |
| AS-01: Twilio number may serve ANITA | Same account, new number | User-provided | user answer D3 |
| AS-02: "separate instances" = logical isolation | Separate DB/index/models/ports, not separate hosts | Assumed | validate Stage 5 |
| AS-03: corpus stays in one collection | 169 chunks, no sharding | Derived | corpus size |
| AS-04: buyers accept an AI assistant | — | Assumed | business risk; `DG-10` |
| AS-05: mock CRM sufficient for v1 | — | User-provided | user brief |
| AS-06: price caveat always | — | Derived | corpus flags |
| AS-07: sentiment re-targets, not rewrites | — | Assumed | TPO feasibility, Stage 6 |
| AS-08: "all platforms" = OS + channels + surfaces + voice | — | User-provided | user answer D1 |
| AS-09: both products share the 16,311 MiB GPU | — | Derived | `.machine_profile.json` |
| AS-10: university deployment stays live | No shared-state migration possible | Derived | user brief |

## 7. Assumptions

- `Assumed: AS-02` — "separate instances" means logical isolation (own DB, Chroma path, collection, model config, ports), not separate physical hosts. Validate before Stage 6.
- `Assumed: AS-04` — buyers will accept an AI assistant for factual catalogue questions. Business risk; mitigation is the human-handoff path (BRD-13).
- `Assumed: AS-07` — the sentiment subsystem re-targets by taxonomy/prompt change. TPO feasibility item in Stage 6; if false, MOD-07 grows a rewrite story.

## 8. Constraints

- **No Salesforce code reuse** (user, explicit).
- **Same Twilio credentials**, new number (D3).
- **Single GPU**: 16,311 MiB shared with the live university assistant; ANITA must fit its own VRAM budget without starving the other product (AS-09).
- **Windows-first** deployment with macOS/Linux parity (D1).
- The corpus is **read-only input** produced by the existing scraper; this plan does not re-crawl.
- Source repo is live and dirty (branch `feat/us014-caller-interruption`) — reuse must **copy**, never modify in place.

## 9. Dependencies

| Dependency | Owner | Risk if unavailable |
|---|---|---|
| Collected corpus (`data/out/`) | This repo's scraper | Blocks all answering |
| Twilio account + new number | ANITA / ops | Blocks all telephony and WhatsApp |
| Ollama + models on the GPU box | Ops | Blocks answers and embeddings |
| Cloudflare tunnel (or fixed ingress) | Ops | Blocks Twilio callbacks — webhooks need public HTTPS |
| PostgreSQL instance | Ops | Leads/sentiment degrade (source system tolerates absence) |
| Additional messaging-channel accounts | ANITA | Blocks the extra channels only |

## 10. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| GPU contention with the live university assistant | H | H | Explicit VRAM budget per product; `MAX_CONCURRENT_CALLS` admission control; measure before enabling outbound |
| Wrong price quoted to a buyer | M | **H** | BRD-05 (SQL not LLM) + BRD-06 (caveat) + BRD-22 (frozen eval set) |
| Corpus drifts from the live TradeIndia catalogue | M | M | Re-run the scraper; corpus carries `crawled_at` and `content_sha256` per row |
| Source repo has no auth — copying that posture publicly exposes lead data | H | H | `DG-06` remediation is **mandatory** for ANITA (public commercial traffic) |
| Secrets copied accidentally from source `.env.bak-*` | M | H | Migration script must whitelist keys explicitly; never copy `.env*` |
| Sentiment re-target proves to be a rewrite (AS-07 false) | M | M | TPO feasibility check in Stage 6 before committing the estimate |
| Additional messaging channels expand scope beyond capacity | M | M | `DG-07` bounds which channels are MVP vs phase 2 |
| Buyers reject the assistant as impersonal | M | M | AS-04 validation; human handoff always one turn away |

## 11. Success Criteria

- A buyer can ask any of the 36 published FAQs on any channel and receive the published answer, grounded and cited.
- A price question returns the exact catalogue figure **with** the quote-required caveat, 100 % of the time.
- The two "Medical Dry Imager" products are never conflated in an answer.
- An unanswerable question produces a handoff record, never an invented answer.
- A negative-sentiment conversation escalates within one turn.
- Both products run concurrently on the box with no cross-instance data leakage and no university-assistant regression.

## 12. Compliance / Regulatory Notes

Medical-equipment marketing is regulated (drug/device advertising rules). This plan treats the assistant strictly as a **catalogue-answering and lead-capture** system: it must not make comparative efficacy claims, must not offer clinical guidance (BRD-10), and must always present prices as indicative. Final wording of the refusal responses requires legal review — `DG-08`.
