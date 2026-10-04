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
| `IN PROGRESS` | **8** | US-001 (see §3a), US-002, US-003, US-008, US-016, US-017, US-018, US-021 |
| `BLOCKED` | **1** | CR-02 (needs decisions Q9–Q12) |
| `YET TO START` | **5** | DG-06 operational config · DG-08 legal review · U-1 · U-2 · U-3 (all in §6) |
| `NOT FEASIBLE` | **0** | Nothing in the plan is infeasible — the feasibility verdict is **Feasible** |

Stories: 22 + 8 = 30. `NOT FEASIBLE` is genuinely zero: `FEASIBILITY-STUDY.md` §1 returns
"Feasible. Recommended, with two blocking conditions before public launch."

**US-001 moved from `BLOCKED` to `IN PROGRESS` on 2026-10-04.** The previous entry said it was
blocked on provisioning a Twilio number. That was incomplete: the number was necessary but nothing
like sufficient — `voice.py` had never executed a single turn. See §3a for what was actually wrong
and what has since been fixed.

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
| US-001 PSTN voice end to end over Twilio Media Streams | `IN PROGRESS` | **Build started 2026-10-04 — see §3a.** Number provisioned (`+12792365960`, E.164). The answer path now works end to end and is proven. Still missing: the three routes (`POST /twilio/voice`, `POST /twilio/status`, `WS /twilio/media-stream`), the TwiML builders, and the media-frame relay — note `voice.py`'s docstring references `handle_media`, which **does not exist anywhere** |
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

## 3a. US-001 build log — started 2026-10-04

### The blocker was not what the previous entry said

The earlier `BLOCKED` entry read "deferred until a Twilio number is provisioned". The number mattered,
but it was not the blocker. `voice.py` is a near-verbatim copy of the source's `voice_handler.py`
(1,890 lines; a CR-stripped diff shows 24 differing lines, all of them import retargets) and it
**had never executed a single turn**. It was not wired to any route, was never constructed by any
test, and carried defects that would have failed on the first call.

### Environment

| # | Problem | Resolution |
|---|---|---|
| B1 | `audioop` was removed from the stdlib in Python 3.13; the venv is **3.14.7**. Every µ-law↔PCM frame conversion calls it | Installed `audioop-lts`, added to `requirements.txt`. **Verified**: 320 B PCM16 → 160 B µ-law → 320 B, RMS 14772 |
| B2 | `scipy` imported unguarded by the TTS chunking path but never declared — it only worked transitively | Installed and declared. **Verified**: `scipy.signal.resample` works, 440 Hz tone survives |
| B3 | **`import av` is blocked by Smart App Control** (`VerifiedAndReputablePolicyState = 1`) — 3 of PyAV's 67 extension modules. The FFmpeg DLLs are unaffected | Decision taken: disable Smart App Control. **Outstanding** — needs the Windows Security UI and a reboot |

**Note on B2:** the `scipy` block was *transient*. Smart App Control blocks freshly-written binaries
until they acquire reputation, so `scipy` failed minutes after `pip install` and passed later
unchallenged. Only PyAV is persistently blocked (installed August, still refused). Worth knowing
before diagnosing a future post-install import failure as a real problem.

### Wiring defects in `voice.py` (Phase C)

| # | Defect | Status |
|---|---|---|
| C1 | `voice_pipeline.run_rag_query_sync` imported as a name the module never defined — it exposed only a factory. The `ImportError` was swallowed, so **every batch turn returned `""`** | ✅ Fixed |
| C2 | Same for `voice_rag.query_rag_stream` — every **streamed** turn returned `("", [])` | ✅ Fixed |
| C3 | `get_whisper_device_config()` returned a 3-key dict where the call site unpacks 2 values. The `ValueError` was swallowed by a broad `except`, falling back to `device = "cuda" if CUDA_VISIBLE_DEVICES else "cpu"` — and that variable defaults to `"0"`, so the fallback was **always CUDA** | ✅ Fixed |
| C4 | `REGISTRY.note_served/note_failed/note_asset_played` did not exist on `AdmissionController` → `AttributeError` on the first completed turn | ✅ Fixed — the four-outcome axis (US-016 AC-2) now lives on the controller |
| C5 | `asset_path()` returns `None` by design, then `None.is_file()` was called outside the try block | ✅ Fixed |
| C6 | `ClauseCutter` API mismatch — the engine read `.text`, ANITA's returns plain `str` | ✅ Fixed |
| C7 | Two separate `AdmissionController` instances (`voice_admission` vs `assembly`), so voice capacity was counted twice and the ceiling would not hold | ✅ Fixed — assembly installs its controller into the engine's seam |
| C8 | `ChannelGateway.public_base_url` is never set by `assembly.py`, so signature validation would compare against `""` | ⬜ Open |

### The answer path — C1/C2 in depth

The adapters were not merely mis-imported. They did retrieval only and returned **raw chunk text**,
while the live chat path (`ChatSurface._answer`) is deterministic-first and never calls the LLM —
`OllamaBackend.generate()` has no callers anywhere in the app.

Wiring voice to the LLM would have made it the **only** LLM prose consumer: answering differently
from chat, uncovered by the frozen evaluation set, and putting generated price text one prompt slip
from violating `BRD-05`.

**Decision (user, 2026-10-04): deterministic-first, with LLM prose only for open questions.**
Implemented by **extracting the answer tree out of `ChatSurface`** into `anita/answer_engine.py`
(`AnswerEngine`), which chat and voice now share:

* `answer_engine.py` — the decision tree plus the reply shape and the canned texts
* `surfaces.py` — `ChatSurface` delegates; the moved names are re-exported so no importer changes
* `voice_pipeline.py` / `voice_rag.py` — both answer through the installed engine
* `assembly.py` — installs the engine into the voice seam at boot

`ChatSurface.answers` / `.retriever` / `.leads` became properties that read through to the engine,
so there is exactly one copy of each and no way for the surface and the engine to disagree about
which corpus is in play.

**Proven working** — a caller's turn now produces:

| Spoken question | Answer |
|---|---|
| "what does the ultrasound gel do" | *"Ultrasound Gel: What makes this Ultrasound Gel suitable for…"* — grounded, names the product |
| "how much is the ultrasound gel" | *"Ultrasound Gel is listed at ₹60/Piece. This is the published list price, excluding taxes; a formal quotation is required for an order."* — price **with** the caveat, no generated text |
| "hello" | greeting |
| "what can you do" | help text |
| "who won the world cup" | decline **with** the callback offer |

The streamed path cuts the same answer at speakable boundaries, so clause-level TTS still starts
early: `['Ultrasound Gel is listed at ₹60/Piece.', 'This is the published list price, excluding taxes;', 'a formal quotation is required for an order.']`

### The media path (`voice_webhook.py`, 2026-10-04)

New module, 17 tests. Implements the ingress order the contract fixes — **signature, then
admission, then TwiML** — and nothing about it needs a carrier to verify:

| Acceptance criterion | How it is proven |
|---|---|
| AC-5 / TAC-4 — invalid signature → 403, empty body, no side effect | Valid, missing, and **tampered-body** signatures all covered; a refused call takes no admission slot |
| AC-4 / TAC-5 — capacity full → busy TwiML, **no stream** | Asserted as `<Stream` absent from the body, and `active_calls()` unchanged |
| TAC-1 — validate before anything else | The body is never parsed before the signature check |
| T-16 — signature is checked against the **public** URL, not the request host | Signed for `https://anita.example.com`, delivered "through the tunnel", still validates |
| US-016 AC-2 — refusals land on the four-outcome axis | `admission.outcomes["refused"]` asserted |
| TwiML well-formedness | Both shapes parsed with `ElementTree`; a quote in the URL cannot break out of the attribute |

**C8 is fixed**: `assembly.py` now reads `TUNNEL_HOST`, builds the public base URL, passes it to
`default_gateway(public_base_url=...)`, and constructs a `VoiceWebhook` — which is exposed as
`Assembly.voice`. An unset `TUNNEL_HOST` is reported as a degradation rather than left implicit.

### The routes are live (2026-10-04)

`POST /twilio/voice` and `POST /twilio/status` are registered in `server.py` and proven over HTTP
with `TestClient` — the thing that catches a handler which is built and wired but never served,
which is exactly the state this was in before. The webhook rides on `Router` (not on `Route`: those
are JSON endpoints, and a Twilio webhook takes a form body and answers in TwiML), so the whole
surface still has one seam and `serve.py` needed no change.

The raw **form fields** are what get validated, because that is what Twilio signs — a handler that
validated a parsed subset would accept bodies Twilio never signed. With no `TUNNEL_HOST` configured
the paths return **404 rather than existing and always refusing**.

Fixed on the way: `app_from_environment()` passed the whole `Assembly` where `create_app` expects a
`Router`, so every request would have raised `AttributeError` on `Assembly.dispatch`. It was dead
code — the live path uses `serve(built.router)` — but it was wrong.

### `.env` was never read — fixed

**`.env` was written, shipped as `.env.example`, declared via `python-dotenv`, and even *inspected*
by `config_truth.audit_config` — but nothing ever loaded it.** `load_dotenv` was not called anywhere
in the repo; `load_settings()` read `os.environ` or an explicit mapping, and nothing bridged them.

The consequence was that every secret in the file was inert: the Twilio credentials, the provisioned
number, and `ANITA_ADMIN_TOKEN`. It is why every boot reported `TWILIO_AUTH_TOKEN unset` while a
valid 32-character token sat in the file — and it would have refused every inbound call at the
signature check, because `validate_twilio_signature` raises when there is no token.

Now wired in `config.py::_environment`, with three properties, each tested:

* `.env` is read **only when no mapping was passed**, so a test or caller supplying its own
  environment is never contaminated by the developer's file
* **Real environment variables win** — the standard dotenv precedence, and the one that matters:
  an override being silently discarded means running with a credential nobody realises is wrong
* A missing or malformed `.env` does not stop the boot; the file is a convenience, and anything
  missing is named by the boot report rather than guessed

**Verified:** a full preflight now reports **one** degradation — `TUNNEL_HOST unset` — down from two.
`TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` (`+12792365960`) and `ANITA_ADMIN_TOKEN` all load.

### The media-stream relay (`media_stream.py`, 2026-10-04)

`WS /twilio/media-stream` is registered and the relay is built. **This is new code, not a port** —
the source's media loop lived in `app/main.py`, which REC-10 dropped by design, and `voice.py`'s
docstring refers to a `handle_media` that was never written.

The relay holds **no decision of its own**. End-of-speech belongs to `feed_audio`; the barge-in
verdict belongs to `admit_inbound_frame` (which already calls `request_interruption()` itself); the
answer belongs to the installed answer engine. `media_stream.py` moves bytes and keeps the protocol
honest — putting a second decision point beside the engine's is exactly the defect US-014 exists to
remove.

| Behaviour | How it is proven (27 tests) |
|---|---|
| `start` captures streamSid / call_id / caller; the engine is given `call_id` | over the socket and directly |
| A duplicate `start` does not restart the conversation | UC-03 duplicate row |
| Payloads are base64-decoded to 160-byte µ-law frames | asserted byte-for-byte |
| End-of-speech sends the answer back as base64 `media` | over the real ASGI socket |
| Streamed audio is **not sent twice** | `process_utterance` returns the same frames it streamed; re-sending would speak every clause twice |
| `mark` every 25 frames (~500 ms), not before | flow control |
| Barge-in frames go to the gate; `FRAME_ECHO`/`FRAME_MUTED` are **not** fed | the assistant must not answer its own voice |
| `clear` is sent so interrupted audio stops playing out of Twilio's buffer | without it, a working barge-in sounds broken |
| `stop`, and an **abnormal drop**, both release the admission slot | AC-8 / SM-04 — a leaked slot makes the box refuse calls while idle |
| `close` is idempotent and never releases another call's slot | |
| No audio is sent after close; late frames are ignored | frames in flight during teardown |
| The call id is masked in logs | TAC-8 / REC-14 |

Two defects were found **by these tests**, not by review: `_on_media` fed frames arriving after
teardown, and a close could have released a slot twice. One test bug of my own was also corrected —
it admitted two calls into a controller whose ceiling was 1.

### Still to build for US-001

1. **The STT leg cannot be exercised yet.** Everything either side of it is proven — the relay
   protocol, the answer path, and Kokoro TTS (RTF 0.60) — but **µ-law → Whisper cannot run while
   Smart App Control blocks `import av`**. Until that is off (B3 above) the full audio round trip
   is untested, and a real call would fail at transcription.
2. **Schema** — `channel_endpoint`, `inbound_event`, `channel_session`.
3. **Public ingress** — a Cloudflare tunnel, which is also what clears the remaining `TUNNEL_HOST`
   degradation. Quick tunnels change hostname on every restart, so Twilio's webhook must be
   re-pointed each time; the source had a `scripts/update_twilio_webhook.py` for this, which is
   **not** in this repo.
4. **Not yet implemented from the story:** the 30-minute call cap (`TAC-6`) and the three-strike
   unintelligible-utterance path (`AC-7`).
5. **Verification** — `T-01…T-17`, then a real call to `+12792365960`.

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
| Unit suite | `.venv/Scripts/python -m pytest tests -q` | **747 passed**, 1 skipped, 0 failed |
| Browser suite | `bash tests/e2e/run.sh` | **50 passed** (25 scenarios × desktop + mobile) in 1.7 m |
| System integrity | `scripts/system_check.py` | **16 passed · 0 failed · 5 deferred** — 38 modules loaded, 11 dependencies wired |
| Evaluation set | `scripts/release_gate.py --no-browser` | **70 / 70 cases passed** |
| Voice answer path | see §3a | grounded answer, price **with** caveat, greeting, help, decline — all correct |
| Release gate | `scripts/release_gate.py` | **FAIL — false alarm.** See below |

The extraction in §3a was verified against all four of the first gates — the unit suite, the frozen
evaluation set, the browser suite and the system check — because it moves live code the evaluation
set depends on. All four stayed green.

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

1. **Finish US-001** (§3a) — the remaining six items: C8, the three routes, the TwiML builders, the
   media-frame relay, the schema, and the tunnel. The answer path is done; this is the transport.
2. **Fix the release gate** (§8.3) — small, contained, and it currently produces a false `FAIL` on a
   suite that is genuinely green. A port preflight that reports *"no instance on :8085 — start
   `run.sh` first"* removes the ambiguity.
3. **Answer CR-02 Q9–Q12** (§5) to unblock the production-infrastructure change request.
4. **Disable Smart App Control** (B3, §3a) — needs the Windows Security UI and a reboot, then a
   re-probe that `import av` succeeds.
5. **DG-06 operational config** — `TWILIO_AUTH_TOKEN` *is* set in `.env`; the remaining work is
   confirming signature verification live once the webhook route exists (item 1).
6. **DG-08 legal review** — needs an owner; blocks public launch and UC-09.

Items 1–3 are engineering and can start immediately. Items 4–6 need either a local machine change or
people outside the build.

**Committed 2026-10-04:** CR-01 (all ten stories, `shared/rag`, `shared/sentiment`, both adapters,
three test files, and the CR-01/CR-02 planning documents) as `5b46528` on
`chore/secret-hygiene-and-config-audit`. That branch name still describes unrelated work; if this
becomes a PR it should be renamed. The Phase C work above is **not yet committed**.
