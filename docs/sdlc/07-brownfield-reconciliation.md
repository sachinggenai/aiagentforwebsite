> **Lens:** TPO + Architect · **Decided by:** TPO (reuse/refactor calls), Architect (boundary calls) · **Engagement:** Brownfield · **Inputs:** `05-modularization.md`, `06-architecture.md`, Stage 0 code reconnaissance · **Defines:** REC-01 … REC-23

# Brownfield Reconciliation — ANITA MEDICAL SYSTEMS Product Marketing & Support Assistant

**Ground rule:** the source repo is **live and dirty** (branch `feat/us014-caller-interruption`, 6 uncommitted files). Everything here is a **copy**, never an in-place edit. The university product must keep running untouched.

**Classification vocabulary:** `reuse` (take as-is) · `refactor` (take, then change) · `relocate` (take, change namespace — it was mis-namespaced) · `pattern-only` (re-implement the idea, do not copy the code) · `drop` (do not carry) · `forbidden` (excluded by the brief).

---

### REC-01 — `app/voice_handler.py` (1,890 LOC) — real-time voice turn loop
**Classification:** `refactor` · **Target:** MOD-05 · **Source:** `app/voice_handler.py`

The real-time turn loop (VAD → STT → RAG → LLM → TTS, with barge-in, echo gate and clause-level TTS) is the single most valuable asset in the source and has **no Twilio imports** — it is already transport-agnostic. Refactor: strip the RAG call (now MOD-02's job) and the CRM session minting, and expose it as speech services only. Rebuilding this loop would be the largest single cost in the project.

---

### REC-02 — `app/crm/session.py` (304 LOC) — conversation identity registry
**Classification:** **`relocate`** · **Target:** MOD-02 · **Source:** `app/crm/session.py`

**Mis-namespaced, not coupled.** Its own docstring says "No network I/O, and no knowledge of the CRM" — it is the conversation-identity registry the whole app depends on. It must survive the CRM cut and move to the orchestrator. Deleting it with the rest of `app/crm/` would break conversation grouping across channels.

---

### REC-03 — Two divergent vector stores
**Classification:** `drop` · **Target:** — · **Source:** local Chroma collection `langchain` (35 docs) + ERC MCP `meridian_kb__v1` (1,144 docs)

The source's own reconciliation flags this as a defect — two stores, silently reconciled by a fallback path. **Do not carry the split.** ANITA starts with exactly one collection, `anita_ams__v1`, and one writer. Carrying this bug into a *7× smaller* corpus would be gratuitous.

---

### REC-04 — `app/rag.py` + `rag_mcp.py` + `rag_legacy.py` + `rag_config.py` (1,714 LOC)
**Classification:** `refactor` · **Target:** MOD-03 · **Source:** `app/rag*.py`

The dispatcher / circuit-breaker / fallback *idea* is sound and battle-tested; the MCP hop is unnecessary for 169 chunks. Refactor to a single in-process hybrid retriever (dense + BM25 + weighted RRF), keeping the breaker and the explicit `not_relevant` signal that BRD-04 depends on.

---

### REC-05 — `app/sentiment/` (1,239 LOC)
**Classification:** `refactor` · **Target:** MOD-07 · **Source:** `app/sentiment/{scorer,categorizer,models,schema}.py`

`AS-07` confirmed by reading the code: LLM-extraction → strict JSON → weighted composite (`SENTIMENT_W1..W4`) → EWMA → category. The domain delta is the **taxonomy and prompt** (admissions emotions/objections → product-enquiry emotions/objections such as price, delivery and specifications), not the architecture. Cheapest high-value reuse in the repo.

---

### REC-06 — `app/crm/` (11 modules except `session.py`, 3,349 LOC)
**Classification:** **`forbidden`** · **Target:** MOD-06 · **Source:** `app/crm/*`

Explicitly excluded by the brief. It is a Salesforce Admission API client (httpx, `CRM_BASE_URL`, `X-API-Key`, SOQL-injection scrubbing, ContentVersion chunked upload, admission-status picklists) — none of the domain vocabulary applies. **Replace with a mock CRM store behind an interface** so a real CRM can be swapped in later (BRD-14).

---

### REC-07 — `app/crm/outbox.py` (282 LOC) — durable retry outbox
**Classification:** `pattern-only` · **Target:** MOD-06 · **Source:** `app/crm/outbox.py`

Excluded as code (it lives under the forbidden package), but the **durable-retry-outbox pattern** — attempts, `next_attempt_at`, a partial index over due rows — is exactly what MOD-01's send retries and MOD-07's notification retries need. Re-implement against the ANITA schema.

---

### REC-08 — `app/llm_backend.py` (734 LOC)
**Classification:** `reuse` · **Target:** MOD-04 · **Source:** `app/llm_backend.py`

The Ollama/MLX provider switch with `pick_model()`, streaming with a `ClauseCutter` for sentence-boundary TTS, and `OLLAMA_KEEP_ALIVE` residency handling. Satisfies the cross-platform requirement (BRD-19) directly. Only the prompt templates change (REC-09).

---

### REC-09 — `app/voice_system_prompt.py` + `rag_legacy.SYSTEM_PROMPT` (755 LOC)
**Classification:** `refactor` · **Target:** MOD-04 · **Source:** `app/voice_system_prompt.py`

The *structure* — numbered grounding rules, the `{context}` slot, the interruption contract, profile/history blocks, retrieval-query decoupling — is directly reusable. The *content* is admissions and 100 % of it must be rewritten for a product catalogue, with the grounding rules tightened (BRD-04, BRD-06, BRD-10).

---

### REC-10 — `app/main.py` (4,133 LOC)
**Classification:** `drop` · **Target:** — · **Source:** `app/main.py`

**The single biggest obstacle and the most important `drop`.** It fuses Twilio webhooks + media-stream loops + the WhatsApp state machine + admission control + CRM session minting + the entire REST API in one file. Carrying it would import the tight coupling this re-architecture exists to remove. Its *behaviour* is re-expressed across MOD-01, MOD-02 and MOD-10.

---

### REC-11 — Twilio integration (7 files, ~1,100 LOC)
**Classification:** `refactor` · **Target:** MOD-01 · **Source:** webhook handlers, `app/messaging.py`, `app/outbound/twiml.py`, `app/outbound/caller.py`, `scripts/update_twilio_webhook.py`

The TwiML templates, Media Streams handling, outbound dialing with status callbacks and the webhook-repointing script are all proven and directly reusable. Refactor: split the webhook layer out of `main.py`, add signature validation (`DG-06` — the source has **none**), and bind to the **new ANITA number** (D3).

---

### REC-12 — `app/outbound/caller.py` + `worker.py` (≈520 LOC)
**Classification:** `refactor` · **Target:** MOD-06 · **Source:** `app/outbound/`

The `call_queue` polling worker with row claiming is exactly the outbound model `SM-04` describes. Refactor onto the ANITA schema and add `FOR UPDATE SKIP LOCKED` to harden the double-dial race the source plan flagged.

---

### REC-13 — Schema DDL patterns (~600 LOC)
**Classification:** `refactor` · **Target:** MOD-06, MOD-07 · **Source:** `app/database.py`, `app/leads/schema.py`, `app/offers/schema.py`, `app/sentiment/schema.py`

The raw-psycopg2 + idempotent-`CREATE TABLE IF NOT EXISTS`-on-boot pattern is reused, along with the graceful-degradation behaviour (every DB helper returns `None`/`False`/`[]` when Postgres is down — a genuinely good property). **Two mandatory changes:** (a) a distinct `anita` schema/database for BRD-18; (b) `app/crm/schema.py`'s `ALL_CRM_SQL` is executed **unconditionally at boot** in the source — carrying that line after deleting `app/crm/` breaks startup. Drop the table shapes with no ANITA meaning (`courses`, `offer_letters`, `lead_documents`).

---

### REC-14 — `app/config.py` + `app/config_truth.py` (1,250 LOC)
**Classification:** `reuse` · **Target:** MOD-09 · **Source:** `app/config.py`, `app/config_truth.py`

A frozen settings dataclass with typed env parsing that names the offending key, **plus** an auditor that detects keys written but never read and masks anything matching `TOKEN|SECRET|PASSWORD|KEY|SID|AUTH`. That auditor is the direct answer to `DG-09`, and the masking is the reason no secret value appears anywhere in this plan. Excise the ~25-key CRM block.

---

### REC-15 — `app/perf_trace.py`
**Classification:** `reuse` · **Target:** MOD-08 · **Source:** `app/perf_trace.py`

Per-stage tracing already exists. Extend to the ANITA stage names and make it the basis of BRD-21. Its best property — being dependency-free so telemetry can never fail a call — is preserved.

---

### REC-16 — `app/dashboard/` + `app/static/` (~1,100 LOC)
**Classification:** `refactor` · **Target:** MOD-10 · **Source:** `app/dashboard/*`, `app/static/{dashboard.html,dashboard.css,dashboard.js,voice_client.html}`

The vanilla-JS "Command Cockpit" (REST + 2 s polling, deliberately **not** SSE because a Cloudflare tunnel returns headers but never the body) and `voice_client.html` (browser mic → WebSocket) are directly reusable for UCs 12/13 and UC-04. Refactor to the ANITA data shapes and add the admin auth `DG-06` requires.

---

### REC-17 — `eval/` harness (~500 LOC)
**Classification:** `pattern-only` · **Target:** MOD-08 · **Source:** `eval/`

Deliberately imports **nothing** from `app.*` — a good pattern worth preserving. The university's 137-case golden set is not transferable; ANITA needs its own frozen set built from the corpus's 36 FAQs plus derived commercial questions (BRD-22).

---

### REC-18 — `bootstrap_services.py` + `start_services.ps1` + `start_services.sh` (~2,400 LOC)
**Classification:** `refactor` · **Target:** MOD-09 · **Source:** `bootstrap_services.py`, `start_services.*`

The multi-phase CHECK → INSTALL → START orchestration with platform branching is real, hard-won work. Refactor for isolation: the source **hard-codes ports** and **clones an external repo at Step 4b** — both must go. All ports become configurable and the isolation guard is asserted at boot (BRD-18).

---

### REC-19 — Admission, VRAM and platform machinery (~1,400 LOC)
**Classification:** `reuse` · **Target:** MOD-09, MOD-05 · **Source:** `app/admission.py`, `app/work_priority.py`, `app/boot_readiness.py`, `app/hardware_profile.py`, `app/memory_budget.py`, `app/platform.py`

Admission control, background-work deferral, boot readiness gating, hardware tiering and the VRAM budget model. These are exactly the mechanisms the Stage 7 scale analysis depends on, and they already understand sharing a GPU — which the ANITA deployment requires (AS-09).

---

### REC-20 — University domain modules (~2,300 LOC)
**Classification:** `drop` · **Target:** — · **Source:** `app/offers/`, `app/emailer.py`, `app/programs.py`, `app/pipeline.py`

`offers` and `programs` are admissions domain objects with no ANITA meaning. `pipeline.py` builds a **Pipecat pipeline that is installed but bypassed** on the live path (confirmed: one caller, a test script) — carrying a dead framework is negative value. `emailer.py` survives only as the `DG-12` notification transport.

---

### REC-21 — `.env`, `.env.bak-pre-us006`, `.env.bak.20260919T041845`, `.env.example`
**Classification:** **`forbidden`** · **Target:** — · **Source:** repo root

**Security.** Three files on disk carry live credential values. The migration must copy **key names only** into a fresh `.env.example`, never a value; the source's `.env.example` is itself non-empty for `DATABASE_URL`. A new Twilio number (D3) means new values regardless.

---

### REC-22 — `tests/` (~45 modules, ~11,000 LOC)
**Classification:** `pattern-only` · **Target:** all · **Source:** `tests/`

The test *structure* (recovery, session, status, WhatsApp flows, RAG dispatcher) maps onto ANITA's use cases one-to-one and is a useful checklist for Stage 9. The tests themselves assert admissions behaviour and Salesforce payloads; none is portable. Note the source suite is **not all green** (5 async tests fail — `pytest-asyncio` missing): do not inherit a red suite as normal.

---

### REC-23 — `content/meridian/meridian_knowledge_base.md` (317 lines)
**Classification:** `drop` · **Target:** — · **Source:** `content/meridian/`

The university knowledge base, replaced entirely by `data/out/` (169 chunks, 24 products). The source's ingestion **validation gate** — refusing a KB that mentions the wrong institution — is a `pattern-only` keep: ANITA's index should equally refuse a corpus that is not the ANITA catalogue.

---

## Net-new (no source equivalent)

| Asset | Module | Why it cannot be reused |
|---|---|---|
| Channel-adapter abstraction + a second messaging adapter | MOD-01 | The source hard-codes its channels in one file; D1 requires a channel addable by configuration |
| Deterministic SQL answer path | MOD-04 | The source answers everything through RAG + LLM; BRD-05 is a new correctness requirement |
| Price-caveat enforcement | MOD-04 | No equivalent — the source has no commercial answers |
| Mock CRM store + CRM interface | MOD-06 | The source has only a Salesforce client |
| Isolation guard (collision refusal at boot) | MOD-09 | The source has no concept of a sibling product |
| Frozen ANITA evaluation set + release gate | MOD-08 | The university's set is not transferable (REC-17) |
| Clinical-refusal path | MOD-02, MOD-04 | No equivalent in an admissions domain |
| Product disambiguation | MOD-02, MOD-04 | New consequence of a catalogue with duplicate product names |

## Coupling traps (verified, must be handled)

1. **`app/database.py` runs `ALL_CRM_SQL` on every boot, unconditionally** — not behind `CRM_ENABLED`. Deleting `app/crm/` without editing this line breaks startup. (REC-13)
2. **`app/crm/session.py` has no CRM dependency** but sits inside the forbidden package. Deleting the package wholesale removes the conversation-identity registry the whole app needs. It must be *relocated*, not cut. (REC-02)
3. **`conversation_id` is load-bearing** — a column added by the CRM migration, now used for logical conversation grouping across WhatsApp messages. Dropping it with the CRM cut requires re-deriving grouping from `conversations.id` + phone + idle window. (REC-13)
4. **The `crm_*` columns are in the core `leads` schema** and are touched by `app/leads/models.py` (~10 accessors), `app/leads/service.py`, `app/offers/service.py` and `app/main.py`. Removing them changes signatures in four modules. (REC-06)
5. **`CRM_ENABLED` defaults `false` in code but is `true` in the local `.env`** — the deployed source instance is live-wired to Salesforce. Behaviour is droppable; structure is not. (REC-06)
6. **`FASTAPI_WORKERS` is set but never read by the launcher** — a live example of the "set ≠ live" defect `DG-09` exists to prevent. (REC-14)
7. **`INTERRUPTION_CONTRACT_BY_FLAG`** — flipping `BARGE_IN_ENABLED` changes the prompt prefix and invalidates the engine's prefix cache. ANITA inherits both, so the coupling is preserved knowingly. (REC-09)

## Reuse economics

| Classification | Approx. LOC | Share |
|---|---|---|
| `reuse` | ~4,300 | 9 % |
| `refactor` | ~12,900 | 28 % |
| `relocate` | 304 | <1 % |
| `pattern-only` (re-implemented) | ~12,000 | — |
| `drop` | ~19,000 | 41 % |
| `forbidden` | ~3,900 (+ ~14 KB of secrets) | 9 % |

**Conclusion:** roughly **37 % of the source carries directly** (reuse + refactor + relocate); the remaining ~63 % is dropped or forbidden. The valuable core — the speech turn loop, the LLM backend abstraction, the sentiment engine, the config auditor, the admission/VRAM machinery and the Twilio glue — survives. The university *domain* (offers, programs, courses, Salesforce, admissions) does not, which is precisely the boundary this re-purpose draws.
