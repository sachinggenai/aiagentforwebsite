> **Stage 0 — Engagement [Lens: all]** · **Decided by:** all lenses (classification + scope mode) · **Engagement:** Brownfield — source repo `D:\project\universityDemo` (Meridian University admissions assistant; `app/` 24,341 LOC, 170 `.py` files, 60,302 LOC total, 218 commits, 8 processes on one Windows box) → target repo `D:\project\anitamedicalsystems` · **Scope:** full delivery plan

# Product Intent — ANITA MEDICAL SYSTEMS Product Marketing & Support Assistant

> **Lens:** PO · **Decided by:** PO, consulted TPO/Architect on MVP feasibility · **Inputs:** user brief (re-purpose `universityDemo`), Stage 0 brownfield evidence, collected corpus at `D:\project\anitamedicalsystems\data\out` (32 pages, 24 products, 169 chunks) · **Defines:** AS-01 … AS-10

---

## Stage 0 — Engagement Record [Lens: all]

### Classification

**Brownfield platform, greenfield product.** The multi-channel voice/chat platform is mature and proven; the business domain it serves changes completely (university admissions → B2B medical-equipment sales and support). Every existing module must therefore be treated as *"prove it fits"*, not *"assume it fits"*.

### Source system (as-is) — evidence

| Property | Finding | Evidence |
|---|---|---|
| Domain | Meridian University admissions voice + chat assistant (legacy UMD/FDU data) | `doc/sdlc/00-product-intent.md`, `content/meridian/` |
| Surfaces | FastAPI backend :8000 (60 routes) · Streamlit student chat :8501 · Streamlit admin dashboard :8502 · legacy CLI bot | `app/main.py` (4,133 LOC), `app.py`, `dashboard.py`, `admissions_bot.py` |
| Stack | Twilio + faster-whisper (STT) + Kokoro (TTS) + Ollama/qwen2.5:14b **or** MLX (Apple) + Chroma/ERC-MCP + PostgreSQL (`pgvector/pgvector:pg16`, db `admissions`) + Redis + FastAPI + Streamlit | `requirements.txt`, `docker-compose.yml`, `.env.example` |
| RAG | `PDF → PyPDFLoader → RecursiveCharacterTextSplitter(1500/100) → OllamaEmbeddings(nomic-embed-text) → ChromaDB → MMR → Qwen2.5`; hybrid dense+BM25+RRF | `doc/RAGPIPLINE/RAG_Pipeline_Report.md` |
| Vector stores | **Two divergent stores** — local `chroma_local_db/` (collection `langchain`) and external `enterprise-rag-core` MCP (collection `meridian-kb`) | `doc/sdlc/07-brownfield-reconciliation.md` REC-03 |
| Modules | MOD-01 Voice Turn Path · MOD-02 Retrieval & Grounding · MOD-03 Inference Serving · MOD-04 Speech Services · MOD-05 Lead & CRM · MOD-06 Observability · MOD-07 Config & Boot | `doc/sdlc/05-modularization.md` |
| Sentiment | **Already implemented** — `app/sentiment/{scorer,categorizer,models,schema}.py` (1,239 LOC), PostgreSQL-backed, EWMA scoring | `app/sentiment/models.py`, `SENTIMENT_W1..W4` keys |
| CRM | **Salesforce-coupled** — `app/crm/` 12 files (3,653 LOC): client, identity, outbox, worker, session, sync, documents, voice | `app/crm/client.py` docstring |
| Voice detail | Hand-rolled `VoiceCallSession` turn loop; VAD → STT → RAG → LLM → TTS; barge-in shipped (`feat/us014`); Pipecat installed but **bypassed** | `app/voice_handler.py` (1,890 LOC), `app/pipeline.py` |
| Hardware | Single Windows workstation, RTX 5060 Ti 16 GB, tier `nvidia_high`, ~6 GB GPU budget for STT+LLM+TTS | `.machine_profile.json` |
| Exposure | Cloudflare quick tunnels (one per port) + `scripts/update_twilio_webhook.py` re-points Twilio on each start | `.tunnel_8501`, `.tunnel_8502`, `.whatsapp_tunnel` |
| Prior plan | Complete SDLC set at `doc/sdlc/00…09` (older format, no `plan-state.md`); its scope was *performance & concurrency of the admissions voice path*, not product definition | `doc/sdlc/` listing |
| Repo state | Branch `feat/us014-caller-interruption`, 218 commits (130 in Sep), working tree **dirty** (6 files) | `git log`, `git status` |

### Target system (to-be) — from the user brief

| Requirement | Source |
|---|---|
| Multi-channel: Streamlit chat + Twilio voice (inbound **and** outbound) + WhatsApp | User-provided |
| **Separate instances** of DB / services / RAG / LLM from the university deployment | User-provided |
| **No Salesforce/CRM code reuse** — a mock CRM database instead | User-provided |
| Sentiment analysis retained for the new use case | User-provided |
| Reuse the **same Twilio credentials** | User-provided |
| "Support all platforms" | User-provided (ambiguous — AS-08) |
| Consumes the already-collected corpus (24 products, 36 FAQs, company facts) | Derived — `data/out/` |

### ⚠ Security finding (surfaced, not acted on)

`D:\project\universityDemo` contains **three `.env.bak-*` files with live credential values on disk** (`.env.bak-pre-us006`, `.env.bak.20260919T041845`, and `.env` itself), all gitignored. The re-purposed deployment must **not** copy them. No secret value was read or copied into any artifact in this plan.

### Inspected (read-only)

`doc/sdlc/00-product-intent.md`, `doc/sdlc/05-modularization.md`, `doc/` inventory, `doc/Sentiment_analysis/`, `doc/RAGPIPLINE/`, `doc/salesforce/`, `app/` tree, `app/main.py` route census, `app/voice_handler.py`, `app/llm_backend.py`, `app/rag*.py`, `app/crm/client.py`, `app/sentiment/models.py`, `requirements.txt`, `docker-compose.yml`, `bootstrap_services.py`, `.env.example` (**key names only**), LOC census, git state.

### Scope mode

**Full delivery plan** — the brief explicitly requests user stories, HLD and LLD.

---

## 1. Problem

ANITA MEDICAL SYSTEMS sells 24 medical-imaging products (CR systems, dry imagers, pressure injectors, X-ray accessories, ultrasound gel, protective apparel) through a **static TradeIndia catalogue**. A buyer — a hospital procurement officer, a radiographer, a diagnostic-centre owner — who wants a specification, a price, an MOQ or a delivery time has exactly one path: submit an enquiry form and wait for a human.

That does not match how this equipment is bought. The questions are **short, factual and constant** — *"what does the FCR Prima T2 weigh?"*, *"MOQ on the ultrasound gel?"*, *"do you supply in Delhi?"* — they arrive outside business hours, and they arrive on whichever channel the buyer is already holding. Every question that goes unanswered is a lead that reaches a competitor who answered first.

## 2. Product Intent

Give ANITA MEDICAL SYSTEMS **one AI assistant that answers product, pricing and company questions instantly across web chat, inbound and outbound voice, and WhatsApp — grounded strictly in the company's own published catalogue — and that qualifies, scores and hands off every interested buyer to a human sales representative.**

It is built by re-homing the proven multi-channel platform from the Meridian admissions assistant onto the medical-equipment catalogue, with its **own isolated database, vector index, model configuration and service ports**, so the two products never share state.

## 3. Target Users & Personas

| Persona | Role | Job-to-be-done | Pain today |
|---|---|---|---|
| **Priya — Hospital procurement officer** | Primary buyer | Get an exact spec, price, MOQ and delivery time for a named product, then a quotation | Form-and-wait; cannot compare two products |
| **Dr. Rao — Radiographer / clinic owner** | Technical buyer | Confirm a machine fits his room, power and workload before enquiring | Specs buried in a long page; nobody to ask out of hours |
| **Meera — Service engineer** | Existing customer | Find the right consumable or accessory for an installed machine | Catalogue is organised by product, not by "what fits my machine" |
| **Arun — ANITA sales rep** | Internal | Receive qualified, contextualised leads — not raw form dumps | Form enquiries carry no qualification, urgency or sentiment signal |
| **Ops / admin** | Runs the stack | Keep four channels healthy; see what buyers ask and how they feel | No cross-channel view, no quality or sentiment signal |

## 4. Success Metrics

| Metric | Type | Target | Classification |
|---|---|---|---|
| **Qualified leads captured / week across all channels** | **North Star** | ≥ 40 by week 12 (baseline ~0 outside the enquiry form) | Assumed |
| Grounded-answer rate (answer traceable to a corpus chunk) | Guardrail | ≥ 98 % | Derived |
| Hallucinated price / spec rate | Guardrail | **0** — a wrong price is a commercial liability | User-provided |
| First-response latency — chat | Guardrail | p95 ≤ 2.5 s | Assumed |
| First-response latency — voice | Guardrail | p95 ≤ 1.2 s (inherits the platform's measured 600 ms endpointing floor) | Derived from `doc/sdlc/00-product-intent.md` |
| Channel coverage | Guardrail | All four channels on one shared answer engine | User-provided |
| **Cross-product isolation** | Guardrail | Zero shared rows, indexes, models or ports with the university deployment | User-provided |
| Sentiment capture | Guardrail | 100 % of conversations scored; negative-sentiment handoff within 1 turn | Derived |
| Escalation correctness | Guardrail | Every unanswerable question produces a handoff, never an invention | Derived |

## 5. MVP Scope

| In | Out |
|---|---|
> **Revised at iteration 1 (Stage 5).** This table was first written before the platform questions were answered. Decision **D1** (all four platform options) and **D3** (new Twilio number) expanded the boundary; rows marked *(D1)* / *(D3)* are the additions. Logged in `04-coverage-gap-analysis.md` §7.

| In | Out |
|---|---|
| Four core channels on one answer engine: Streamlit/web chat, Twilio voice in, Twilio voice out, WhatsApp | Payments / online ordering |
| **Browser-mic voice** on the website alongside PSTN *(D1)* | Voice cloning / custom voice identity |
| **One additional messaging-channel adapter**, built as a configurable pattern with a single reference implementation *(D1)* | A bespoke adapter for every conceivable messaging provider |
| **Responsive / mobile-web chat surface** (web-grade, no native app) *(D1)* | Native iOS / Android applications |
| **Minimal admin console** — channel health, live conversations, conversation review (UC-12, UC-13) *(D1; required by BRD-21)* | Full CRM-style analytics / BI dashboards |
| RAG over the collected corpus (24 products, specs, prices, trade terms, 36 FAQs, statutory company facts) | Multi-language & Hinglish at launch (architecture must permit it) |
| Deterministic SQL lookups for price / MOQ / delivery / GST / address | Salesforce or any real CRM — **mock CRM DB only** |
| Sentiment scoring + negative-sentiment escalation | Quotation document generation & e-signature |
| Lead capture, qualification and handoff record | Re-using any existing Salesforce client code |
| Mock CRM database reusing the outbox/idempotency *pattern*, not the Salesforce code | Sharing a database, vector index or model process with `universityDemo` |
| Isolated DB / Chroma index / model config / ports per product | Cloud / multi-region deployment |
| **Own Twilio number and WhatsApp sender on the shared account** *(D3)* | Re-pointing the university's existing number |
| Auth, rate limiting and Twilio webhook signature validation on public surfaces (`DG-06`) | Enterprise SSO / multi-tenant identity |
| Deployment on the existing Windows + RTX 5060 Ti box | |
| Platform support: Windows (primary) + macOS / Apple Silicon (MLX path retained) + Linux/Docker | Mobile native apps |

## 6. Non-goals

- **Not** a clinical or diagnostic advisor — the corpus is a product catalogue, not medical guidance; clinical questions must be refused and escalated.
- **Not** a replacement for the TradeIndia catalogue — a conversational layer over it.
- **Not** a general-purpose chatbot — off-catalogue questions are declined, not improvised.
- **Not** a CRM — the mock CRM exists to prove the integration seam, not to run a pipeline.
- **Not** a shared multi-tenant deployment — each business gets isolated instances.

## 7. Assumptions

| ID | Assumption | Classification |
|---|---|---|
| AS-01 | The Twilio number(s) configured for `universityDemo` may legitimately serve ANITA traffic (same account, additional use) | User-provided |
| AS-02 | "Separate instances" means separate database, Chroma path, collection names, model config and ports — not necessarily separate physical hosts | Assumed — validate Stage 5 |
| AS-03 | The 24-product corpus (169 chunks, ~20.5 K tokens) stays fully resident in one vector collection with no sharding | Derived |
| AS-04 | Buyers will accept an AI assistant for factual catalogue questions, with human handoff for anything commercial | Assumed — business risk |
| AS-05 | A mock CRM DB is sufficient for launch; no real CRM integration is required in v1 | User-provided |
| AS-06 | Every price answer must carry the list-price / quote-required caveat (corpus records `is_list_price=true`, `quote_required=true`) | Derived |
| AS-07 | The existing sentiment subsystem (1,239 LOC) re-targets from admissions to product conversations by taxonomy and prompt change, not a rewrite | Assumed — TPO feasibility item |
| AS-08 | "Support all platforms" means the **runtime** platforms (Windows + macOS), **not** extra chat channels beyond the four named | Assumed — **highest-risk ambiguity; confirm before Stage 6** |
| AS-09 | Both products may run concurrently on the shared 16,311 MiB GPU; VRAM must be budgeted for both | Derived |
| AS-10 | The university deployment stays live and unchanged while the ANITA instance is built — no shared-state migration is possible | Derived |
