> **Lens:** TPO (sequencing) · **Updated:** 2026-10-04 · **Scope:** the fifteen `IN PROGRESS` stories of US-001…US-030, carried to `COMPLETED` with evidence.
> **Companion to:** `work-status.md` (status ledger — §9 mirrors this sequence), `plan-state.md` (plan checkpoint). Derived from the 2026-10-04 call-site audit; every "where" below was verified in code.

# Completion Plan — the fifteen open stories

## 1. What this plan is for

`work-status.md` §3 records fifteen stories as `IN PROGRESS`: each has code, tests, and a gap
between "built" and "reachable". This document is the sequenced route through that gap — the order,
the dependencies, what "done" means per story, and the risks that could change the order.

**Scope (user decisions, 2026-10-04):**

| Decision | Choice | Consequence |
|---|---|---|
| US-014 / US-015 | **Wire the LLM prose leg** | The backend is constructed at assembly, open questions use generated prose through `prompts.py` + the numeric-grounding check; deterministic answers stay first for commercial intents |
| US-002, US-003, US-008 | **Build now** | The UC-02/UC-04 deferrals in `config/anita.yaml` are removed as each becomes driven; the WhatsApp webhook route and the browser audio path are in scope |
| University-domain cleanup | **Tasks in `work-status.md` §10** | Not separate story files; the tasks carry file:line, action and acceptance check |
| Sequencing | **Foundations → live fixes → voice core → carrier proof → channels/state → retrieval** | §5 |

**Done for the whole plan** = all fifteen rows read `COMPLETED` in `work-status.md` §3, the
UC-02/UC-03/UC-04 deferrals are gone (UC-09/UC-14 remain, both deliberate), and the release gate
passes against a real instance.

---

## 2. The fifteen, and the one-line reason each is open

| Story | Why it is `IN PROGRESS` | Proof it is done |
|---|---|---|
| US-025 | Trace module complete; `.emit()` has no call site and `new_trace(call_id, turn)` raises `TypeError` into a swallowed `except` — `self._trace = None` every turn | A real chat turn and a simulated call each write a trace row; stages and timings present |
| US-027 | Eval set regenerated from the corpus at boot; `load_eval_set()` has no caller; promotion never driven through `validate=` | A frozen set file with a pinned hash is what the gate runs; `IndexLifecycle.promote` consumes the result |
| US-017 | Barge-in does not cancel queued clauses; `clear_playout()` has no caller | Real-session test: interrupt during clause 1 → clauses 2+ are not synthesised; `clear` frame sent |
| US-022 | `handoff_id=""` → one handoff per DB; no notification; no admin view | Second escalation for a different lead opens a second handoff; email sent; `state='notified'`; admin list works |
| US-018 | `.degrade(` has no runtime caller; no pressure monitor; CPU-host refusal claim unimplemented | A forced-pressure test drives the ladder; admission honours `voice_capable`; the boot claim is true or deleted |
| US-014 | LLM prose unreachable — no backend is constructed | An open question is answered by generated prose that passes the grounding check; commercial intents never reach the model |
| US-015 | Breaker only exists inside `OllamaBackend`, which nothing constructs | With the backend live, an unreachable model degrades prose and records `failed`; SQL answers still serve |
| US-001 | No real carrier call; WS admission unchecked | A real call to `+12792365960` completes T-01…T-17; a direct WS connect without an admitted call is refused |
| US-002 | WhatsApp adapter registered; no webhook route; `ChannelGateway.accept` has no runtime caller | `POST /twilio/whatsapp` answers with TwiML `<Message>`; UC-02 driven in `system_check` |
| US-003 / US-008 | Ticket served, no audio consumer; mic bytes discarded | A browser speaks a question and hears the answer; the ticket is redeemed by the audio socket |
| US-004 | No live path passes a cross-channel identity | Chat and voice for the same phone number resolve to one conversation; identity survives restart |
| US-006 | In-memory transcript, append-level locking; interleaving reproduced | Transcript persists across restart with ordering; concurrent turns do not interleave |
| US-010 | `IndexLifecycle` promotion has no production caller | The live build path runs through `validate=`; a failing validation refuses promotion |
| US-012 | Single `source_url`; no `CoverageProbe` | Replies carry chunk-level citations; absence is probe-verified |

---

## 3. Ordering principles

1. **Foundations before wiring.** US-025 and US-027 are the evidence system. Wiring new live paths
   before the trace emits and the eval set is frozen means every later change is evidenced by
   something that can silently change under it.
2. **Live-path defects before the carrier proof.** US-001's `T-01…T-17` is the expensive,
   hard-to-repeat test. Barge-in (US-017) and admission (US-018) are part of what it measures, so
   they land first or T-02/T-17 must be run twice.
3. **One ordering constraint is inherited and must not be violated:** persistence changes land on
   SQLite behind the existing Protocol seam, because CR-02 moves them to Postgres and doing both at
   once makes every regression ambiguous (work-status §5).
4. **Deferrals are removed only when the use case is driven.** Removing UC-02/03/04 makes the
   release gate demand the feature; that is the point, and it is why each removal is the *last* step
   of its story, not the first.
5. **Every story ends with evidence, not assertion:** the exact command and its real output recorded
   in `work-status.md` and, for CR-01-adjacent work, `change-requests/STATUS.md`.

---

## 4. The tracks

Sizes are relative (S ≈ 1–2 days, M ≈ 3–5, L ≈ 6–10) and assume one engineer; tracks are
parallelisable where §5 shows no arrow between them.

### Track A — Foundations (do first)

| Story | Work | Exit criteria | Size |
|---|---|---|---|
| **US-025** trace | Fix the signature mismatch at `voice.py:1137` (either make `new_trace(**kwargs)` accept positional or change the call); restore `mark()`/`note()`/`stages_seen` on `TurnTrace` or adapt the call site; add `.emit()` calls on the chat path (`surfaces.py`) and the media-stream path (`media_stream.py`); add the TAC-3/TAC-5 size/rotation rules; a broken trace must still never drop a call | One chat turn and one simulated call each produce a trace record containing `vad_end`/`stt_done`/`llm_sent`/`llm_done`/`tts_first_chunk` (voice) and their chat equivalents; the writer's failure mode is tested | S–M |
| **US-027** eval set | Write `platform/data/eval/anita_eval_v1.jsonl` from the current 70 cases; pin its SHA-256 in `config/anita.yaml`; make `assembly` load it via `load_eval_set()` and refuse a hash mismatch; add a *reviewed* regeneration script (never automatic); drive `IndexLifecycle.promote` with `validate=` in the live build path; fix the `QualityGate` false-green at `observability.py:362` | The gate runs the frozen file, not `cases_from_corpus()`; changing the corpus stops changing the set; a failed validation refuses promotion; the regression-floor branch is covered by a test | M |

### Track B — Live-path fixes

| Story | Work | Exit criteria | Size |
|---|---|---|---|
| **US-017** barge-in | Check the cancel flag inside the clause loop and inside `_synthesise_streamed` (`voice.py:1550-1594`); call `media_stream.clear_playout()` from the relay when `request_interruption()` wins; add a real-session (not `FakeSession`) barge-in test | Interrupt after clause 1 → one sink call, `clear` frame on the wire, first-audio budget for the restarted answer measured from the trace | S–M |
| **US-022** handoff | Make `crm.record_handoff` assign `handoff_id` when empty (UUID), and key idempotency on `(lead_id, state)` — not on the id; pass the real lead id (not conversation id) at `surfaces.py:225`; build the notifier: SMTP send + `state='notified'`/`notified_at`, best-effort with recorded failure; add an authenticated admin route listing handoffs | Second escalation for a different lead opens a second handoff; a notification is attempted and its outcome recorded; no-notifier and broken-SMTP paths do not fail the turn | M |

### Track C — Voice core, then the carrier proof

| Story | Work | Exit criteria | Size |
|---|---|---|---|
| **US-018** admission | Register a real pressure source for `gpu_free_mib` at assembly (only where a GPU exists); add the controller policy that calls `degrade()` on pressure and `recover()` when it eases; make admission consult `voice_capable` — either refuse voice on a non-voice-capable host or cap it (CPU hosts demonstrably transcribe and synthesise, so "cap at 1" is the honest reading); then either implement or delete the boot claim at `boot.py:196-200` | A forced-pressure test walks the ladder and a recovered test walks back; a torch-less host behaves as the boot report says; the relay's release path is unchanged | M |
| **US-014 + US-015** prose + breaker | Construct the backend at assembly (`OllamaBackend` behind the existing `LLMBackend` seam); route **open questions only** through `prompts.build_prompt` + `verify_answer` + `ClauseCutter`; keep price/MOQ/delivery/spec deterministic-first (BRD-05); with the backend live the breaker is exercised — on open, prose declines with the handoff line and `complete_turn(failed=True)` records it; report breaker state to metrics/console | An open question is answered with generated text that passes numeric grounding; a fabricated figure is rejected; a commercial question never reaches the model; with Ollama stopped, prose declines and SQL still answers; the trace shows the breaker state | L |
| **US-001** carrier proof | Add the WS admission check (`start` must name an admitted `call_id`); build the load harness (three concurrent simulated calls using the same client that produced `CA-simcall-1`); run `T-01…T-17`; place a real call to `+12792365960`; move to a named tunnel | All `T-01…T-17` pass with output recorded; a direct WS connect with an unadmitted id is refused; a real CallSid appears in `inbound_event`; work-status §3a is updated | M–L |

### Track D — Channels (parallel with C after B)

| Story | Work | Exit criteria | Size |
|---|---|---|---|
| **US-002** WhatsApp | Serve `POST /twilio/whatsapp` beside the voice webhook (form body, raw-field signature via `ChannelGateway.accept`, both registers); dispatch the envelope through the orchestrator and answer the webhook with TwiML `<Message>` (no outbound REST needed); register `TWILIO_WHATSAPP_NUMBER` (currently a declared-but-pending key); remove the UC-02 deferral; add the driven check to `system_check` | A signed inbound webhook returns `<Message>` with a grounded answer; a replayed `MessageSid` is refused; `system_check` drives UC-02 and it passes with the deferral removed | M |
| **US-003 + US-008** browser mic | New `WS /voice/browser`: the client redeems its ticket on connect, streams 20 ms PCM16 frames (AudioWorklet), reuses `VoiceCallSession` unchanged, and plays returned PCM through an AudioWorklet sink; barge-in uses the same `admit_inbound_frame` seam; the page keeps the keyboard composer one tap away (US-008 AC); admission counts browser sessions; remove the UC-04 deferral and add a driven check | A browser speaks a question and hears the answer; typing stays one tap away; barge-in works; the ticket is single-use and the socket refuses an unredeemed/expired ticket; the CPU cost of one browser caller is measured against the 2-call ceiling | L |

### Track E — State

| Story | Work | Exit criteria | Size |
|---|---|---|---|
| **US-006** transcripts | Introduce a `ConversationStore` Protocol (the US-115 pattern: injected, never global) with a SQLite implementation behind the existing store seam; persist conversations, turns with sequence numbers, and routing; serialise the **whole turn** (answer included) per conversation; back the admin read path with the store; summary-on-close | Two threads on one session cannot interleave; a restart preserves transcripts; admin review reads from the store; the store unavailability degrades rather than drops a turn | M |
| **US-004** identity | Pass identity on every live path: web chat (session + captured phone/email), WhatsApp (`From` number — already parsed at `channels.py:258`), voice (Twilio `From`, read at `media_stream.py:133` and currently discarded); link identities to one conversation when a lead is captured; make the registry reconstruct from the store on restart | One buyer over web chat then voice resolves to one conversation with a linked transcript; identity survives restart; withheld-number calls keep a stream-SID identity | M |

### Track F — Retrieval and answer surface

| Story | Work | Exit criteria | Size |
|---|---|---|---|
| **US-010** index lifecycle | Route the live build through `IndexLifecycle` + `build_index(..., validate=)` so promotion is gated by the frozen eval set; apply the `anita.index_builds` DDL (or drop the design deliberately — record which); add an operational rebuild entry point that refuses to promote on failure and leaves the previous index serving | A rebuild with a failing validation leaves the old index serving; a passing rebuild promotes atomically; the build history is queryable | M |
| **US-012** provenance | Carry chunk-level citations (chunk id + source URL + content hash) from the retriever through `ChatReply` to the API; add the `CoverageProbe` for absence claims; handle `citation_incomplete` | A price answer cites its chunks; an absence answer is probe-backed; the admin review shows citations | M |

---

## 5. Sequence

```
Batch 0  US-025 ──→ US-027  ✅ done 2026-10-04  (foundations; also unblocks C evidence)
              │
Batch 1       ├─→ US-017  US-022  ✅ done 2026-10-04  (small live fixes; parallel)
              │
Batch 2       ├─→ US-018 ──→ US-014 + US-015  ✅ done 2026-10-04  (voice core)
              │
Batch 3       ├─→ US-001       ▶ next              (carrier proof: needs 017, 018, 025)
              │
Batch 4       ├─→ US-002  ✅ done 2026-10-04   (WhatsApp + extra channel + outbox)
              └─→ US-003+008  ✅ done 2026-10-04 (browser audio: transport + client)
Batch 5            US-006 ──→ US-004  ✅ done 2026-10-04 (state; reusable store seam)
Batch 6            US-010  US-012  ✅ done 2026-10-04 (retrieval; US-010 consumes the US-027 gate)
```

**Hard dependencies:**

* US-027 → US-010 (promotion consumes the frozen set) and US-027 → every story that changes live
  wiring (the eval set is the release evidence).
* US-025 → US-001 T-17 and US-017 (first-audio budgets are read from traces).
* US-017, US-018 → US-001 (T-02 and T-17 measure them).
* US-006 → US-004 (identity links are persisted in the same store).
* US-014 → US-015 (the breaker is only exercised once the backend exists; same change set).

**Parallelism:** Track D and Track E can run beside Track C without touching its files. Track F
starts after US-027 and is otherwise independent.

---

## 6. Obligations that apply to every story

1. **Evidence line.** Record the command and its real output in `work-status.md` (and
   `change-requests/STATUS.md` where CR-01 code is touched — US-015 and US-022 both do).
2. **Gates.** `pytest tests -q` green; `scripts/system_check.py` with no new deferrals;
   `scripts/release_gate.py` against a running instance for anything that changes live wiring.
3. **Deferral hygiene.** Removing a UC from `release.deferred_use_cases` is the last step, and it is
   what makes the use case *required* from then on.
4. **Doc truth.** Every story that changes a live path updates §3 of `work-status.md` in the same
   change set — the audit found the previous drift came from code and status being updated in
   separate commits.
5. **No university-domain text** may enter a changed file (see `work-status.md` §10).

---

## 7. Risks and open questions

| # | Risk | Where it bites | Mitigation |
|---|---|---|---|
| R1 | **Generated prose on a live path** (US-014) | A model slip could state a price without the caveat or invent a figure | Commercial intents never reach the model; `verify_answer` rejects ungrounded numbers; the frozen eval set gains prose cases before the story closes |
| R2 | **CPU-bound voice** | Kokoro synthesises at ~0.6× real time; a browser caller (Track D) adds load beside two phone slots | Admission counts browser sessions; measure one browser caller against the ceiling before switching UC-04 on |
| R3 | **A real call depends on things outside the repo** | Tunnel, Twilio console, a phone | Keep the simulated-call harness as the repeatable evidence; treat the real call as the final gate, not the development loop |
| R4 | **WhatsApp sandbox vs production** | `TWILIO_WHATSAPP_NUMBER` is a declared-but-pending key; Twilio sandbox semantics differ | Land the route against a signed fixture first (as the voice webhook was); the number is ops, not code |
| R5 | **Persistence drift before CR-02** | US-004/US-006 add SQLite tables that CR-02 must later move | Behind Protocols, as US-115 established; noted in the CR-02 ordering constraint |
| R6 | **The release gate's blind spot** | It cannot tell "no instance" from "broken", and builds with an empty environment (`work-status.md` §8.5, §8.19) | Fix the gate as part of Batch 0 — it is the evidence channel for everything after |

---

## 8. Definition of done for this plan

- [ ] Fifteen rows in `work-status.md` §3 read `COMPLETED`, each with an evidence line.
- [ ] `config/anita.yaml` → `release.deferred_use_cases` contains only UC-09 and UC-14.
- [ ] `system_check.py` drives UC-02, UC-03, UC-04 and reports them `PASS`.
- [ ] The release gate passes against a running instance (after the §8.19 fix).
- [ ] A real Twilio call is recorded in `inbound_event` with a real CallSid.
- [ ] `work-status.md` §10 cleanup tasks are all checked.
