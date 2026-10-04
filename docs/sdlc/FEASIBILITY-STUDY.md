> **Lens:** TPO + Architect · **Decided by:** TPO (feasibility), Architect (technical risk), PO (business risk) · **Inputs:** the full Stage 0–9 artifact set · **Companion to:** `plan-state.md`

# Feasibility Study — Re-purposing `universityDemo` for ANITA MEDICAL SYSTEMS

> This document answers the question the SDLC artifacts imply but do not state directly: **should we do this, how long will it take, and what will bite us?** All figures trace to the reconnaissance in `00-product-intent.md` (Stage 0) and `07-brownfield-reconciliation.md` (REC-01…REC-23).

---

## 1. Verdict

**Feasible. Recommended, with two blocking conditions before public launch.**

The source system already solves the two hardest technical problems in this product: **real-time voice with barge-in on a single GPU**, and **channel-agnostic conversation handling**. What it does *not* have is a product-catalogue domain, an isolated deployment, or any security posture — and those are exactly the boundaries this plan draws.

| Question | Answer |
|---|---|
| Is the technology proven? | **Yes.** Voice, WhatsApp, chat, RAG and sentiment all run end-to-end in production today on the same hardware. |
| Is the reuse real or aspirational? | **Real, ~37 % of the source**, verified file-by-file in `07-brownfield-reconciliation.md`. |
| Is the domain change large? | **Yes for content, no for architecture.** 100 % of prompts and taxonomy change; almost no structural change is needed in the reused modules. |
| Can both products coexist? | **Yes, with a caveat** — they share one 16,311 MiB GPU, which is the binding constraint (CV-01, AS-09). |
| What must be true before launch? | `DG-06` (auth, rate limiting, Twilio signature validation) and `DG-08` (legal review of clinical-refusal wording). Both are **blocking**. |

---

## 2. What was analysed

| Source | Size | What it told us |
|---|---|---|
| `D:\project\universityDemo` | 60,302 LOC Python, 170 files, 218 commits, 146 markdown docs | The platform |
| `doc/sdlc/` (existing plan) | 10 artifacts, 7 modules, 18 stories | The source's own architecture and known defects |
| `doc/RAGPIPLINE/` | ~290 KB | RAG design history — **partly stale**; code is authoritative |
| `.env.example` | 71 keys | The full integration surface (names only; no values read) |
| `D:\project\anitamedicalsystems\data\out` | 169 chunks, 24 products, 10 tables | The corpus this product answers from |

**Note:** `C:\project\universityDemo` is an empty directory — only the `D:\` copy exists. `universityDemo_working28_aug` is an older snapshot missing `app/crm/`, `app/config_truth.py` and other modules; it was ignored.

---

## 3. Capability feasibility

| Capability | Feasibility | Basis |
|---|---|---|
| Inbound voice (PSTN) | **Proven** | Runs today; hand-rolled turn loop with VAD, barge-in, echo gate (`app/voice_handler.py`) |
| Outbound voice | **Proven** | `call_queue` + polling worker + status callbacks already work (`app/outbound/`) |
| WhatsApp | **Proven** | Inbound webhook + outbound send + media handling exist |
| Web chat | **Proven** | Streamlit + REST backend exist |
| Browser-mic voice | **Proven** | `voice_client.html` + `/ws/voice` exist (D1 asks for this — it is already built) |
| Additional messaging channel | **Straightforward** | New; the adapter pattern is a modest extension, not a research problem |
| Mobile-web surface | **Low risk** | Existing static surfaces need responsive work only |
| RAG over the ANITA corpus | **Straightforward** | 169 chunks vs the source's 1,144 — 7× smaller, and retrieval is not the bottleneck |
| Deterministic price/MOQ lookups | **New, low risk** | The corpus already ships indexed numeric columns; this is a new query path over existing data |
| Sentiment | **Proven, needs re-targeting** | 1,239 LOC already implements extraction → composite → category (AS-07) |
| Mock CRM | **New, low risk** | Replaces a forbidden dependency with a local store behind an interface |
| Instance isolation | **New, medium risk** | Needs an enforced guard, because the failure mode is silent (wrong answers, not a crash) |
| Admin console | **Proven** | The source's vanilla-JS cockpit is directly reusable |
| Windows + macOS/Linux | **Proven** | The source already dual-paths Ollama and MLX with platform detection |

**Nothing in the MVP requires technology that does not already work somewhere in the source.**

---

## 4. Reuse economics

| Classification | Approx. LOC | Share | Examples |
|---|---|---|---|
| `reuse` (take as-is) | ~4,300 | 9 % | `llm_backend.py`, `config.py` + `config_truth.py`, `perf_trace.py`, admission/VRAM machinery |
| `refactor` (take, then change) | ~12,900 | 28 % | `voice_handler.py`, `rag*.py`, `sentiment/`, Twilio glue, dashboard |
| `relocate` (mis-namespaced) | 304 | <1 % | `app/crm/session.py` — real code sitting in the forbidden package |
| `pattern-only` (re-implement) | ~12,000 | — | outbox, eval harness, test structure |
| `drop` | ~19,000 | 41 % | `app/main.py` (4,133), `offers/`, `programs.py`, Pipecat |
| `forbidden` | ~3,900 + 3 secret files | 9 % | `app/crm/` Salesforce client, all `.env*` |

**~37 % carries directly.** The five highest-value assets, in order:

1. **`app/voice_handler.py`** (1,890 LOC) — real-time voice turn loop, **no Twilio imports**, already transport-agnostic. This is the crown jewel; rebuilding it would be the single largest cost in the project.
2. **`app/llm_backend.py`** (734 LOC) — Ollama/MLX switch with streaming, clause-level TTS cutting and model residency handling. Satisfies the cross-platform requirement for free.
3. **`app/sentiment/`** (1,239 LOC) — the entire scoring model; only the taxonomy changes.
4. **`app/config.py` + `app/config_truth.py`** (1,250 LOC) — typed config **plus an auditor that detects dead keys and masks secrets**. This is unusually good and directly enables `DG-09`.
5. **Admission / VRAM machinery** (`admission.py`, `work_priority.py`, `memory_budget.py`, `hardware_profile.py`) — already understands sharing a GPU, which the ANITA deployment requires.

---

## 5. Effort estimate

Derived from the 10 modules in `05-modularization.md` and the 30 stories in `stories/`. Ranges assume one experienced developer already familiar with the source.

| Phase | Modules | Stories | Estimate |
|---|---|---|---|
| **P0 — Foundation** | MOD-09 (config/isolation/boot) | 3 | **1–1.5 weeks** — must be first; everything else depends on the isolation guard |
| **P1 — Answer engine** | MOD-03, MOD-04 | 6 | **2–3 weeks** — RAG re-target, deterministic SQL path, caveat enforcement, prompts |
| **P2 — Conversation core** | MOD-02 + MOD-01 (text channels only) | 6 | **2.5–3.5 weeks** — orchestrator, identity, web chat + WhatsApp + signature validation |
| **P3 — Voice** | MOD-05 + MOD-01 (voice paths) | 5 | **1.5–2 weeks** — reuse `voice_handler`; new Twilio number wiring |
| **P4 — Lead & sentiment** | MOD-06, MOD-07 | 6 | **2–2.5 weeks** — mock CRM, outbound, sentiment re-target, escalation |
| **P5 — Surfaces & observability** | MOD-10, MOD-08 | 7 | **2–2.5 weeks** — mobile web, admin console, traces, eval set + gate |
| **P6 — Hardening** | — | — | **1–2 weeks** — `DG-06` security, load testing, `DG-08` legal wording, go-live |
| **Total** | **10 modules** | **30 stories** | **≈ 12–16 weeks** for one developer |

**Compression opportunities:** the sentiment re-target (P4) and the browser-mic client (P3/P5) are near-free if the source modules port cleanly, which could pull the estimate toward **10 weeks**. **Expansion risks:** `DG-06` security work is unbounded if the deployment is exposed publicly without a fixed ingress, and the additional messaging channel is deliberately under-specified (`DG-07`).

---

## 6. Risk register (ranked by expected cost)

| # | Risk | Likelihood | Impact | Mitigation | Traced to |
|---|---|---|---|---|---|
| 1 | **GPU contention** with the live university product — both voice stacks want the same card | High | High | Explicit per-product VRAM budget; admission control; measure before enabling outbound; degrade voice before text | AS-09, CV-01, MOD-05 scale row |
| 2 | **No security posture** — the source has zero auth, no rate limiting, no webhook signature validation | Certain (it is the current state) | High | `DG-06` is **blocking**; TPO owns it in MOD-01/MOD-09 with the buyer/admin split from CV-03 | DG-06, BRD-13 |
| 3 | **Wrong price quoted** to a buyer | Medium | High (commercial) | SQL-only pricing (BRD-05) + mandatory caveat (BRD-06) + frozen eval gate (BRD-22) | CV-03, MOD-04 |
| 4 | **Silent isolation failure** — shared collection name serves the university's answers | Medium | High | MOD-09 refuses to boot on any collision (BRD-18) | MOD-09 |
| 5 | **Scope creep from D1** — "more messaging channels" is unbounded | Medium | Medium | `DG-07` bounds it to one reference adapter + a configurable pattern | DG-07 |
| 6 | **Buyers reject the assistant** as impersonal | Medium | Medium | AS-04 validation; human handoff always one turn away; sentiment trend as adoption proxy | AS-04, DG-10 |
| 7 | **Corpus drift** — the TradeIndia catalogue changes and answers go stale | Medium | Medium | Re-run `src/ams/`; `content_sha256` + `crawled_at` per row; WF-03 re-validates before promotion | WF-03, SM-05 |
| 8 | **Secrets copied** from the source's `.env.bak-*` files | Low | High | Migration copies key **names** only; REC-21 marks all `.env*` forbidden | REC-21 |
| 9 | **Clinical-refusal wording** fails legal review | Low | High | `DG-08` blocking; conservative default in place | DG-08, BRD-10 |
| 10 | **Sentiment re-target is a rewrite, not a config change** (AS-07 false) | Low | Medium | TPO feasibility check before P4 commits | AS-07, REC-05 |

---

## 7. What the source got right (and must be preserved)

Worth stating explicitly, because these are easy to lose in a re-write:

- **Graceful database degradation** — every DB helper returns `None`/`False`/`[]` when Postgres is down, so the app still answers calls. Rare and valuable.
- **Dependency-free telemetry** — `eval/checks.py` imports nothing from `app.*`; the source plan's `TRD-20` makes tracing unable to fail a call. ANITA keeps this.
- **The config auditor** — detects keys written but never read. Most projects discover this in production.
- **Fail-safe classification** — the source's short-utterance gates and confidence floors exist because phone audio produces garbage. Keep them.
- **Absent vs zero** — the source plan insists a missing measurement must appear as *absent*, never as `0`. ANITA's console inherits this.

## 8. What must NOT be carried

- `app/main.py` (4,133 LOC) — the fused monolith that this re-architecture exists to undo.
- `app/crm/` — forbidden by the brief, and domain-inapplicable (Salesforce admissions vocabulary).
- The **two divergent vector stores** — the source's own plan flags this as a defect; a 169-chunk corpus needs one store.
- **Pipecat** — pinned, installed, and bypassed on the live path. Dead weight.
- **`.env*` files** — three of them on disk carry live credential values.

---

## 9. Open decisions the plan deliberately did not make

| # | Decision needed | Owner | Blocks |
|---|---|---|---|
| 1 | Real traffic volume and concurrency target | ANITA / PO | Accurate sizing (`DG-01`); the design survives without it, the *numbers* do not |
| 2 | Which additional messaging channel ships first | ANITA | `DG-07` — a provider choice, not an architecture choice |
| 3 | Legal sign-off on clinical-refusal wording | ANITA / legal | Go-live (`DG-08`) |
| 4 | Fixed ingress (domain + TLS) vs Cloudflare quick tunnels | Ops | The source re-points Twilio webhooks on every restart because quick-tunnel hostnames change — acceptable for a demo, poor for production |
| 5 | Where sales-rep handoff notifications go | ANITA sales | `DG-12` defaults to email |
| 6 | Whether the university product and ANITA run concurrently at peak, or ANITA is time-boxed | Ops | The GPU budget (AS-09) |

---

## 10. Recommendation

**Proceed.** Start with **P0 (MOD-09)** — not because it is user-visible, but because the isolation guard and config audit are what prevent the two products from silently corrupting each other, and retrofitting them later means re-testing everything.

Then **P1 (MOD-03/04)** — the answer engine, because it can be built and validated against the existing corpus *without any telephony*, giving an early, honest signal on answer quality before the expensive channel work begins. That is also the phase that produces the frozen evaluation set, which gates every later release.

**Defer voice until P3.** It is the most impressive channel and the least risky to delay: text channels prove the answer engine, the lead pipeline and the sentiment loop, and voice then reuses a module that already works.

**Do not start P2 until `DG-06` has an owner.** Shipping public chat and WhatsApp without rate limiting or webhook validation on a commercial lead-capture system is the one choice here that could cause real harm.
