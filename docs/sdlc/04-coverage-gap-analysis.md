> **Lens:** BA + TPO + PO · **Decided by:** BA (coverage), TPO (fix feasibility), PO (accepted-gap sign-off) · **Inputs:** `00-product-intent.md`, `01-brd.md`, `02-use-cases-workflows.md`, `03-data-state-analysis.md` · **Engagement:** Brownfield · **Defines:** CV-01 … CV-03

# Coverage & Gap Analysis — ANITA MEDICAL SYSTEMS Product Marketing & Support Assistant

## 1. Requirement → Use Case Coverage Matrix

| Requirement | Use Cases | Workflows | Stories | Tests | Coverage |
|---|---|---|---|---|---|
| BRD-01 Single answer engine | UC-01, UC-02, UC-03, UC-04, UC-14 | WF-01 | planned | planned | UC-covered |
| BRD-02 Cross-channel continuity | UC-01, UC-02 | WF-01 | planned | planned | UC-covered |
| BRD-03 Twilio isolation, own number | UC-03, UC-09, UC-14 | WF-01, WF-02 | planned | planned | UC-covered |
| BRD-04 Grounded answers only | UC-01, UC-02, UC-03, UC-04, UC-15 | WF-01 | planned | planned | UC-covered |
| BRD-05 Deterministic commercial lookups | UC-05, UC-06 | WF-01 | planned | planned | UC-covered |
| BRD-06 Mandatory price caveat | UC-05 | WF-01 | planned | planned | UC-covered |
| BRD-07 Specification answering | UC-01, UC-06 | WF-01 | planned | planned | UC-covered |
| BRD-08 Company and contact answering | UC-01, UC-02 | WF-01 | planned | planned | UC-covered |
| BRD-09 Same-named product disambiguation | UC-01, UC-05, UC-06 | WF-01 | planned | planned | UC-covered |
| BRD-10 Clinical question refusal | UC-11 | WF-01 | planned | planned | UC-covered |
| BRD-11 Lead capture on every channel | UC-08 | WF-01 | planned | planned | UC-covered |
| BRD-12 Lead qualification | UC-08, UC-10 | WF-01, WF-02 | planned | planned | UC-covered |
| BRD-13 Human handoff | UC-07 | WF-04 | planned | planned | UC-covered |
| BRD-14 Mock CRM seam | UC-08, UC-09 | WF-01, WF-02 | planned | planned | UC-covered |
| BRD-15 Sentiment scoring | UC-10 | WF-01 | planned | planned | UC-covered |
| BRD-16 Negative-sentiment escalation | UC-10 | WF-04 | planned | planned | UC-covered |
| BRD-17 Objection capture | UC-10 | WF-01, WF-04 | planned | planned | UC-covered |
| BRD-18 Instance isolation | UC-12, UC-13 | WF-03 | planned | planned | UC-covered |
| BRD-19 Cross-platform runtime | UC-03, UC-04, UC-14 | WF-03 | planned | planned | UC-covered |
| BRD-20 Config single source of truth | UC-12 | WF-03 | planned | planned | UC-covered |
| BRD-21 Per-conversation observability | UC-12, UC-13 | WF-01, WF-02 | planned | planned | UC-covered |
| BRD-22 Quality gate on answer correctness | UC-15 | WF-03 | planned | planned | UC-covered |
| BRD-23 Outbound follow-up | UC-09 | WF-02 | planned | planned | UC-covered |

**Result: 23/23 requirements have use-case coverage. No empty Use Cases cells.**

## 2. Actor Coverage

| Actor | Use Cases | Coverage |
|---|---|---|
| Buyer | UC-01, UC-02, UC-03, UC-04, UC-05, UC-06, UC-07, UC-08, UC-10, UC-11 | ✓ |
| Outbound contact | UC-09 | ✓ |
| Sales rep | UC-07, UC-09, UC-13 | ✓ |
| Ops admin | UC-12, UC-13, UC-14, UC-15 | ✓ |
| Twilio gateway | UC-02, UC-03, UC-09 (participant) | ✓ |
| Messaging provider | UC-14 (participant) | ✓ |
| Background worker | UC-09, UC-10 (participant) | ✓ |

**Result: every actor has ≥1 use case.**

## 3. MVP Scope Coverage

Every "In" item from `00-product-intent.md` §5 (as revised at iteration 1), copied verbatim.

| MVP In item (from 00 §5) | BRD-xx / UC-xx | Coverage |
|---|---|---|
| Four core channels on one answer engine: Streamlit/web chat, Twilio voice in, Twilio voice out, WhatsApp | BRD-01, BRD-02, UC-01, UC-02, UC-03 | ✓ |
| **Browser-mic voice** on the website alongside PSTN *(D1)* | BRD-01, BRD-19, UC-04 | ✓ |
| **One additional messaging-channel adapter**, built as a configurable pattern with a single reference implementation *(D1)* | BRD-01, BRD-19, UC-14 | ✓ |
| **Responsive / mobile-web chat surface** (web-grade, no native app) *(D1)* | BRD-01, UC-01 | ✓ |
| **Minimal admin console** — channel health, live conversations, conversation review (UC-12, UC-13) *(D1; required by BRD-21)* | BRD-21, UC-12, UC-13 | ✓ |
| RAG over the collected corpus (24 products, specs, prices, trade terms, 36 FAQs, statutory company facts) | BRD-04, BRD-07, BRD-08, UC-01 | ✓ |
| Deterministic SQL lookups for price / MOQ / delivery / GST / address | BRD-05, BRD-06, UC-05, UC-06 | ✓ |
| Sentiment scoring + negative-sentiment escalation | BRD-15, BRD-16, BRD-17, UC-10 | ✓ |
| Lead capture, qualification and handoff record | BRD-11, BRD-12, BRD-13, UC-07, UC-08 | ✓ |
| Mock CRM database reusing the outbox/idempotency *pattern*, not the Salesforce code | BRD-14, UC-08, UC-09 | ✓ |
| Isolated DB / Chroma index / model config / ports per product | BRD-18, UC-12, UC-13 | ✓ |
| **Own Twilio number and WhatsApp sender on the shared account** *(D3)* | BRD-03, UC-03, UC-09 | ✓ |
| Auth, rate limiting and Twilio webhook signature validation on public surfaces (`DG-06`) | BRD-13, BRD-21, UC-12 | ✓ |
| Deployment on the existing Windows + RTX 5060 Ti box | BRD-19, UC-12 | ✓ |
| Platform support: Windows (primary) + macOS / Apple Silicon (MLX path retained) + Linux/Docker | BRD-19, UC-03, UC-14 | ✓ |

**Scope-creep check:** nothing in `00-product-intent.md` §6 (Non-goals) has entered the plan. Payments, clinical advice, real CRM, native apps, multi-tenant, quotation generation and voice cloning appear in no requirement, use case or module.

## 4. Scenario Coverage Rollup

| Use case | Scenarios covered | Missing |
|---|---|---|
| UC-01 | 14/14 | — |
| UC-02 | 14/14 | — |
| UC-03 | 14/14 | — |
| UC-04 | 14/14 | — |
| UC-05 | 14/14 | — |
| UC-06 | 14/14 | — |
| UC-07 | 14/14 | — |
| UC-08 | 14/14 | — |
| UC-09 | 14/14 | — |
| UC-10 | 14/14 | — |
| UC-11 | 14/14 | — |
| UC-12 | 14/14 | — |
| UC-13 | 14/14 | — |
| UC-14 | 14/14 | — |
| UC-15 | 14/14 | — |

**Result: 15/15 use cases at 14/14 scenarios.**

## 5. Contradiction Detection

| ID | Statement A | Statement B | Why they conflict | Resolution |
|---|---|---|---|---|
| CV-01 | `BRD-18` requires zero shared rows, indexes, models or ports with the university product | `D3`/`AS-09` share the Twilio account and the single 16,311 MiB GPU | "Isolation" cannot mean physical separation for the account or the card | **Resolved (Architect):** isolation is defined as *logical* — own database/schema, own Chroma collection, own model configuration and service ports. The Twilio account and the GPU are explicitly shared resources, governed by admission control. `AS-02` remains a Stage 5 validation target. |
| CV-02 | `BRD-02` requires a buyer who continues on another channel to be identifiable and keep context | Web chat and browser-mic sessions can be fully anonymous — no phone number is ever given | Continuity is impossible without a correlating identity | **Resolved (PO):** continuity applies **where an identity exists** (phone number, WhatsApp handle, or a persisted session token). Anonymous sessions are conversation-scoped and merge only if the buyer later supplies contact details, at which point `SM-01`'s dedupe key links them. `BRD-02` scoped accordingly. |
| CV-03 | `DG-06` requires auth and rate limiting on public surfaces | `BRD-13` requires buyers to reach a human from a public channel with no friction | Auth on the buyer path would block the very traffic the product exists to serve | **Resolved (TPO):** split the surfaces. **Admin** surfaces (UC-12, UC-13, UC-15) get authentication. **Buyer** surfaces stay open but get per-session rate limiting and Twilio webhook signature validation, so abuse is bounded without adding buyer friction. |

## 6. Gap List

| Gap | Found where | State |
|---|---|---|
| The intent's MVP scope predated the user's platform decisions (D1/D3) — browser voice, extra channel, mobile-web, admin console and Twilio-number isolation were missing | §3 MVP coverage vs `00 §5` | **resolved** in iteration 1 — `00-product-intent.md` §5 revised, `01-brd.md` BRD-01/03/19/21 confirmed to cover it |
| `BRD-22` (quality gate) had no use case — a release-process requirement with no actor | §1 matrix (first pass) | **resolved** in iteration 1 — UC-15 added, WF-03 ties it to the refresh flow |
| The source system has no auth, rate limiting or Twilio signature validation (`DG-06`) | `03-data-state-analysis.md` A.4 | **accepted** — blocking for public launch; TPO owns the fix in MOD-01/MOD-09 (CV-03 sets the buyer/admin split) |
| Traffic volume and concurrency target unknown (`DG-01`) | A.2 load model | **accepted** — PO sign-off: launch at the inherited 2-call ceiling, instrument, revisit with real data |
| Clinical-refusal wording not legally reviewed (`DG-08`) | A.4 | **accepted** — blocking for go-live; PO/legal owns sign-off before public launch |
| Buyer acceptance of an AI assistant unvalidated (`AS-04`, `DG-10`) | `01-brd.md` §7 | **accepted** — PO sign-off: human handoff always one turn away; sentiment trend is the adoption proxy |
| 3 products have no specifications and 4 have no About text on the source site (`DG-13`) | `03-data-state-analysis.md` A.3 | **accepted** — BA/PO: answered as "not published" with a handoff; never inferred (`BRD-07`) |
| No LLM fallback beyond the deterministic path (`DG-03`) | A.4 | **accepted** — TPO: SQL-backed commercial answers survive an LLM outage; prose declines with handoff. Documented, not papered over |
| Outbound cadence and calling window unspecified (`DG-04`) | A.4 | **resolved** in iteration 2 — conservative defaults set (3 attempts, 10:00–19:00 IST, suppression honoured) |
| Sales-rep notification channel unspecified (`DG-12`) | A.4 | **resolved** in iteration 2 — default SMTP email, configurable |
| Which additional messaging channel ships first (`DG-07`) | A.4 | **resolved** in iteration 2 — one reference adapter, others by configuration; `BRD-19` scoped to the pattern, not a provider list |

## 7. Iteration Log

| Iteration | What was added | Where |
|---|---|---|
| 1 | First full pass across all four matrices. Three gaps found: (a) MVP scope predated platform decisions D1/D3 — browser voice, extra channel, mobile-web, admin console and Twilio-number isolation were uncovered; (b) `BRD-22` had no use case; (c) `DG-04`/`DG-07`/`DG-12` had no decisions. Also produced CV-01…CV-03. | — |
| 2 | Added UC-15 (release validation) and revised `00-product-intent.md` §5 to cover D1/D3. Set the three undecided `DG` gaps to derived defaults. Confirmed `BRD-01`/`BRD-03`/`BRD-19`/`BRD-21` already covered the expanded scope, so no new requirements were needed. Scoped `BRD-02` per CV-02. | `02-use-cases-workflows.md`, `00-product-intent.md`, `03-data-state-analysis.md`, `01-brd.md` |
| 3 | Re-ran all four matrices after the edits: 23/23 requirements covered, 7/7 actors covered, 15/15 MVP items covered, 15/15 UCs at 14/14 scenarios, every `DAT-xx` has a decision or a classification, every `SM-xx` has recovery transitions. No new gaps. Challenge run (§8) produced no open concern. **Frozen.** | — |

## 8. Discovery Challenge

1. **Which persona has no meaningful use case?** — None. All three buyer personas resolve to the Buyer actor with UCs across every channel; Arun → Sales rep (UC-07, UC-09, UC-13); Ops → UC-12…UC-15.
2. **Which requirement has no workflow?** — None. Every `BRD-xx` reaches a `WF-xx`; `BRD-22` runs inside WF-03.
3. **Which workflow has no failure path?** — None. WF-01…WF-04 each carry an exception drill and a partial-completion matrix.
4. **Which business rule is never exercised by any use case?** — None. R-1→UC-05, R-2→UC-01/02/03, R-3→UC-06, R-4→UC-11, R-5→UC-10, R-6→UC-09, R-7→UC-01.
5. **Which data element is assumed available but not proven?** — Traffic volume (`DG-01`) and buyer acceptance (`AS-04`). Both accepted with an owner; neither is load-bearing for the *design*, only for sizing.
6. **Which state has no recovery path?** — None. Every `SM-xx` has a resume or compensation transition; `EXHAUSTED`, `SUPPRESSED`, `REJECTED`, `CLOSED`, `WITHDRAWN` are explicitly terminal.
7. **Which external dependency can fail — and what is the plan?** — Twilio (outbox retry + suppression honoured); LLM/embeddings (deterministic path survives, prose declines — `DG-03`); Postgres (degrade to conversation-only, replay on recovery, inheriting the source pattern); messaging provider (adapter marked degraded, other channels unaffected). All four have a documented path.
8. **What happens during duplicate submission?** — Covered explicitly: WhatsApp dedupes on provider message id (UC-02), leads dedupe on normalised phone/email (SM-01), outbound rows are claimed with `FOR UPDATE SKIP LOCKED` (SM-04), evaluations are idempotent (UC-15).
9. **What happens if the user abandons the workflow?** — Conversation goes IDLE then CLOSED (SM-02); a lead already captured persists with its partial data and a follow-up flag (UC-08); no half-written record is left.
10. **What happens if data is stale?** — The corpus carries `crawled_at` and `content_sha256` per row, and WF-03 rebuilds and re-validates before promotion (SM-05). A stale index is never promoted; the previous one stays live.
11. **What happens if two operations occur concurrently?** — Enumerated per state machine: lead merge on unique key (SM-01), turn serialisation (SM-02), composite under row lock (SM-03), one dial per lead (SM-04), atomic index swap (SM-05), one handoff per lead (SM-06).
12. **Which story cannot be independently tested?** — None at this stage; every UC's scenario table is the test floor and Stage 9 gives each story its own LLD test scenarios. This is re-checked at G9.
13. **Which architecture decision has no measurable justification?** — Only two decisions are currently qualitative: the 2-call concurrency ceiling (inherited, not measured for ANITA — `DG-01`) and the idle-window default (6 h, inherited). Both are marked `Assumed:` and carry NFRs from the load model so they can be measured rather than argued.
14. **Which requirement is impossible to verify?** — `BRD-04` (grounded only) is verifiable by asserting every emitted answer cites a corpus record — UC-15 mechanises it. `BRD-18` (isolation) is verifiable by asserting no shared collection name, schema, port or log path exists. Neither is unverifiable.
15. **Which module breaks first at 10× load — and what exactly saturates?** — The **speech module's GPU residency** saturates first, not the request rate: STT, TTS and the LLM share one card with the live university assistant. At 10× the queue depth grows on voice admission while chat and WhatsApp stay comfortable. Detailed per-module in `06-architecture.md`.
16. **Is there a quantitative load model, or are the numbers assumed?** — There is a model (`03-data-state-analysis.md` A.2) with per-flow peak QPS, peak:average and 12-month growth — but **every number is `Assumed:`**, because no ANITA traffic data exists. They are Stage 5 validation targets, explicitly not evidence.
17. **What degrades first under overload — and is that designed?** — Voice admission degrades first, by design: the caller hears a busy message and is offered a callback (UC-03 E1, `SM-04`). Chat and WhatsApp keep serving. Recovery is automatic when capacity frees.
18. **Does anything fall outside the MVP boundary or into a non-goal?** — No. The §3 scope-creep check confirms no Non-goal (payments, clinical advice, real CRM, native apps, multi-tenant, quotation generation, voice cloning) appears in any requirement, UC or module.

## 9. Freeze Decision

**STATUS: COMPLETE**

Every `BRD-xx` (23/23) has use-case coverage with no empty cells; every actor (7/7) has use cases; every MVP "In" item (15/15, post-revision) is covered with a `BRD-xx`/`UC-xx` citation and nothing from the Non-goals leaked in; every use case (15/15) carries 14/14 scenarios; every `DAT-xx` has a completeness classification and every required-but-unavailable asset has a `DG-xx` decision; every key entity has an `SM-xx` with all ten questions answered and a recovery path; the load model is quantified per core flow (with its numbers honestly marked `Assumed:`); and the Discovery Challenge produced no open concern.

Three contradictions were found and resolved rather than left standing (CV-01 isolation-vs-shared-resources, CV-02 continuity-vs-anonymous-sessions, CV-03 auth-vs-public-access). Seven gaps were accepted with a named owner and rationale; none is hidden.
