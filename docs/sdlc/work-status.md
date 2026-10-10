> **Lens:** TPO · **Updated:** 2026-10-10 (seventh pass — every headline number re-run on the unchanged tree, then every story re-derived from call sites; one story demoted and restored, seven defects added and **all seven fixed the same day**, leaving no open engineering item) · **Scope:** every workstream — the SDLC plan (Stages 0–11), the D4 build (US-001…US-030), CR-01 (US-101…US-115), CR-02, and the open items.
> **Companion to:** `plan-state.md` (plan checkpoint) and `change-requests/STATUS.md` (CR-01 build checkpoint). This file is the **whole-of-work** view across all of them.

# Work Status — ANITA MEDICAL SYSTEMS

## 0. How to read this — and how the statuses were derived

**No story file records its own status.** All 30 files in `stories/` were checked: there is no
`Status:` field, no YAML frontmatter, no `## Status` heading, no checked `- [x]` box. Every
occurrence of the word "status" in that directory is code-level (a DB column, an enum value).

The statuses below are therefore **derived from build evidence**, in this order of precedence:

1. The module exists in `platform/app/anita/` and its test file passes.
2. **A live caller actually reaches it** — a module that exists, or that only tests construct, is
   not `COMPLETED`. "Reachable" was verified by tracing call sites, not by trusting the docstring.
3. The deferrals declared in `config/anita.yaml` → `release.deferred_use_cases`, with their reasons.
4. `scripts/system_check.py` and `scripts/release_gate.py` output.

Where code is migrated but not wired, this file says so rather than recording it as done. That
distinction is the whole point of the exercise: the plan-level scorecard (`08-coverage-verification.md`)
is at 100 %, and it explicitly states it "measures *plan* completeness, not product readiness".

**Sixth pass (2026-10-04, late evening): the whole file was re-audited against the code, not just
the code against the file.** Every story row and every defect-register entry below was checked
again by tracing call sites, constructing the objects, reading the DDL and querying the live
stores. The result:

* **One story moved, and moved back:** `US-010` returned to `IN PROGRESS` — its rebuild never
  embedded into the staging collection it names, so the frozen evaluation set scored the
  *previous* index and the promoted collection was filled in later, unvalidated (`§8.23`). That
  defect was **fixed the same day** and the story re-promoted to `COMPLETED`: see the US-010 row
  and §8.23 for the fix and the end-to-end evidence.
* **The register was re-checked entry by entry.** Four entries were stale in the "still open"
  direction — `8.7`, `8.8`, `8.9` and `8.12`'s index-lifecycle clause: the code fixes each of them,
  and the fifth pass had not marked it. The rest are accurate as written: `8.6` and `8.10` are
  genuinely open, `8.13`–`8.17` are genuinely still stale *documents*, and `8.18`–`8.22` are
  genuinely resolved.
* **Seven fresh findings** are added as `8.23`–`8.29`, and the fifth pass's line references were
  corrected where they had drifted (`US-001`'s routes, `US-016`'s STT chain).

*(Sixth pass, 2026-10-04.)* Two independent runs backed the numbers in §7 at that pass:
`pytest tests -q` → **1132 passed, 1 skipped** and
`scripts/system_check.py` → **20 passed · 0 failed · 0 warnings · 1 deferred**; the release gate
re-ran the frozen set at **70/70** with **55 grounded answers, 0 uncited, 0 incomplete**.

**Seventh pass (2026-10-10): the tree was not touched, so this pass re-ran the numbers and re-traced
the stories rather than re-reading them.** The tree is byte-identical to the sixth pass's commit
(`085b68f`) — six days of no commits — which makes this the cleanest test the file has had: any
number that moved, moved because the last pass was wrong, not because the code changed.

* **Every headline number reproduced exactly.** `pytest tests -q` → **1163 passed, 1 skipped**;
  `system_check.py` → **20/0/0 · 1 deferred**; `release_gate.py --no-browser` → **70/70** with
  **55 grounded · 0 uncited · 0 incomplete**; the config audit → **43 live · 5 pending**, the five
  being `DATABASE_URL` + `DB_*`; boot preflight → **OK**, one degradation (SMTP). The frozen set's
  SHA-256 still matches its pin (`eb2ffc27…`), the corpus still holds 141 chunks, and the serving
  index is still `anita_ams__v5` with 141 vectors. §7 carries the runs.
* **`US-017` was demoted to `IN PROGRESS` — and restored to `COMPLETED` the same day (§8.30).** It
  is the same class of finding as US-010's: the mechanism was built and tested, the *live trigger was
  missing*. `voice.py:776 mark_speaking()` had **no caller anywhere in the repository**; it is the
  only writer of `STATE_SPEAKING` and the only caller of `note_playback()`. `admit_inbound_frame`
  returned `FRAME_LISTENING` unconditionally (`voice.py:945`), so the barge-in gate could never
  return `FRAME_INTERRUPTION`, and everything downstream of it — `clear_playout`,
  `note_answer_truncated`, the engine's cancel signal — was unreachable from a telephone call.
  `end_turn()` was dead for the same reason. **Fixed below.**
* **The §8.6 gate was real but partial — fixed the same day.** The WS-admission gate closed a
  tokenless `wss://` connection **that sent `start`**; a stream that went straight to `media` frames
  was never refused, because `self.refused` is only ever set inside `_on_start`. `_on_media` now
  refuses `media before start`. §8.31 has the reproduction.
* **Five more defects** (`8.32`–`8.36`) and one validator failure class, all recorded below and all
  closed the same day. None of them is a regression — the code is the sixth pass's code, and the
  sixth pass's own register said it was clean. They are what a second audit of the same tree finds.
* **Three stale line references and two internal contradictions were corrected** in place
  (`US-011`, `US-016`, `US-025`; §7's "36 keys live"; §8.15's open marker against §9's "done").

The honest summary of the seventh pass: **the product is where the sixth pass said it was, minus one
story — and that story is back.** US-017's barge-in did not work on a phone call because nothing ever
told the engine it was speaking; §8.30 wires that, §8.31 closes the gate's frame-path hole, and both
landed the same day with reproductions that fail without them (see below).

**§8.30 and §8.31 — fixed 2026-10-10.** The demotion stood for a few hours. The trigger is now wired
at the one choke point every outbound frame passes through: `send_frames` calls `mark_speaking()` on
the turn's **first** batch — the moment the carrier starts playing us out — and `note_playback()` on
every batch after it, and `_run_turn`'s `finally` calls `end_turn()` on every exit path (answered,
barge-in, failed, dropped). Both relays do it, because they are two transports for one engine. On the
engine side `end_turn()` no longer clears `_turn_began_as_interruption`: the relay's teardown runs
*before* the next utterance is read, so clearing it there erased the only record that the caller
spoke over the answer; the flag is now consumed exactly once, where it decides the min-burst floor.
**A latent leak went with it** — the flag had no live clearer at all, so one interruption would have
lowered the noise floor for every later utterance on the call. §8.31 makes a stream that sends `media`
without ever sending `start` refused (`media before start`) instead of reaching the engine with no
token, no identity and no slot. **Evidence:** `pytest tests -q` → **1176 passed, 1 skipped** (13 new);
`system_check.py` → **20 · 0 · 0 · 1 deferred**, unchanged; and every reproduction was probed by
reverting each half of the fix and watching the new tests fail (3, 2, 1 and 1 tests respectively).

**§8.32–§8.36 — all five closed the same day.** None needed a decision, which is the point: the
seventh pass's finding was that this file's open list had stopped being "things only a person can
do". The status callback releases the slot only on a **terminal** `CallStatus`; the citation rule
became one function that the **promotion path** and the release gate both run, so a rebuild can no
longer be promoted on answers the gate would have failed; the janitor's summary retry rebuilds a
conversation **from the store**, which is what makes it work after a restart and on the rows
`sweep_idle` closes for threads this process never held; a refused boot of **any** kind writes no
report, not just an isolation refusal; and the two runtime artefacts are **untracked** with the
README's count corrected. **Evidence:** `pytest tests -q` → **1194 passed, 1 skipped** (18 more);
`release_gate.py --no-browser` → **70/70**, 55 grounded · 0 uncited · 0 incomplete, unchanged; and
each of the four code fixes was probed by reverting it — **6, 4, 1 and 1** tests fail respectively.
One environment note: `system_check` reports **19 passed · 0 failed · 1 deferred** rather than 20,
because UC-04 is now `NOT TRIGGERABLE HERE — voice shed at level voice_shed`. That is the **VRAM
ladder working**, not a regression: `nvidia-smi` shows **446 MiB free of 16 311** against a
configured `sibling_floor_mib: 6000`, because the sibling product's Ollama models are resident on
the card. Sampling the policy directly logs `capacity degrade → voice_shed`.

*(Sixth pass, retained as the record of how the current labels were derived.)* **That revision was
the result of a call-site audit of the whole platform (2026-10-04, evening).**
Every story was re-derived by tracing who constructs it and who calls it from a served route or the
media-stream path — not by reading module docstrings. Four stories that were previously `COMPLETED`
were demoted there because their live path does not exist (`US-010`, `US-012`, `US-014`, `US-015`),
along with four more whose wiring is broken or partial (`US-004`, `US-006`, `US-025`, `US-027`).
The audit also produced a defect list — see §8. Nothing in this file should be read as worse news
than it is: the *product* works; what changed is the honesty of the label on each story.

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
| `COMPLETED` | **38** | 28 of US-001…US-030 · 10 of US-101…US-115 · Stages 0–11 |
| `IN PROGRESS` | **1** | US-001 — the transport is built, the WS-admission gate now refuses a stream that never sends `start` (§8.31) and `scripts/carrier_acceptance.py` drives Twilio's half of the acceptance; the one remaining item is the **real carrier call** and its handset half (§3a) |
| `BLOCKED` | **2** | US-021 (outbound worker — held for DG-08) · CR-02 (needs decisions Q9–Q12) |
| `YET TO START` | **4** | DG-08 legal review · U-1 · U-2 · U-3 (all in §6). DG-06's code *and* config are now complete — only the real-call confirmation in §3a remains |
| `NOT FEASIBLE` | **0** | Nothing in the plan is infeasible — the feasibility verdict is **Feasible** |

Stories: 14 + 15 + 1 = 30. `NOT FEASIBLE` is genuinely zero: `FEASIBILITY-STUDY.md` §1 returns
"Feasible. Recommended, with two blocking conditions before public launch."

**US-001 moved from `BLOCKED` to `IN PROGRESS` on 2026-10-04.** The previous entry said it was
blocked on provisioning a Twilio number. That was incomplete: the number was necessary but nothing
like sufficient — `voice.py` had never executed a single turn. See §3a for what was actually wrong
and what has since been fixed. As of this revision the whole transport is built and has carried a
**simulated call end to end over the public tunnel**; what is still missing is a real carrier call
(§3a).

**Why the completed count fell from 32 to 24.** Nine stories that the previous revision marked
`COMPLETED` are now `IN PROGRESS` or `BLOCKED` because the 2026-10-04 audit traced call sites
rather than reading modules: US-004, US-006, US-010, US-012, US-014, US-015, US-025 and US-027 were
built and tested but have no live path (or a broken one), and US-021 was re-classed `BLOCKED`
because it is deliberately held for the legal review. Conversely US-016 was promoted to
`COMPLETED`: the media-stream relay now reaches the real STT leg, and a simulated call proved it
end to end. The product itself did not regress — the labels caught up with the code.

**And why it is back up to 38 — and back down to 37.** The demotions were a to-do list, not a
verdict. US-014/US-015 (prose leg), US-002/US-003/US-008 (messaging + browser voice),
US-004/US-006 (identity + persistence) and US-012 (provenance) have since been built, wired to a
served route, driven live, and re-promoted — each with the demotion's own complaint as the
acceptance test. US-010 was re-promoted in the fifth pass, **demoted again in the sixth** on new
evidence — the staging collection it names was never written — and **fixed the same day**: the
embed and the gate now target the collection the build names (§8.23), proven by a rebuild that
promoted `anita_ams__v5` with 141 vectors embedded and validated — **re-confirmed in the seventh
pass**, where the serving process still logs `serving promoted collection anita_ams__v5` and the
store still holds its 141 vectors.

**The seventh pass demotes US-017** by the same rule the sixth pass used on US-010: the code is
built and its tests pass, but the live path that would trigger it does not exist. The clause loop
honours the cancel signal, the relay handles the interruption transition, and both are covered by
tests that call `request_interruption()` directly — but nothing in the product ever calls
`mark_speaking()`, so the engine never enters `SPEAKING`, the gate never returns
`FRAME_INTERRUPTION`, and a caller who talks over the assistant is simply buffered and answered
afterwards (§8.30). Two stories are therefore `IN PROGRESS`: **US-001** (transport built and
exercised by a simulated call; a real carrier call outstanding) and **US-017** (barge-in mechanism
built; its trigger missing).

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

### 3.1 MOD-01 — Channel Gateway

| Story | Status | Comment |
|---|---|---|
| US-001 PSTN voice end to end over Twilio Media Streams | `IN PROGRESS` | **Transport complete and exercised by a simulated call — see §3a.** All three routes are served (`POST /twilio/voice` at `server.py:165`, `POST /twilio/status` at `:169`, `WS /twilio/media-stream` at `:321` — the fifth-pass refs `159,163,169` were stale), the webhook validates the raw signed form and admits before TwiML (`voice_webhook.py:145-195`; the sixth pass's `:128-178` stopped short of the admission call and the TwiML return), the relay decodes µ-law frames and drives the engine (`media_stream.py:204-264` — `_on_media`; the sixth pass's `:181-245` named `_refusal` instead), and the channel tables give replay protection and attribution (`channel_store.py`). `logs/instance.log.err` records a full simulated call through the public tunnel: 80 frames → real faster-whisper transcript → deterministic answer → Kokoro TTS → clean close. **What is missing is a real carrier call** and its `T-01…T-17` evidence; no real 34-character CallSid appears in any log or table (**re-verified seventh pass by exhaustive scan** — 66 `inbound_event` rows, 61 of them the synthetic `CA`+zeros+hex shape `carrier_acceptance.py` mints). **The WS admission gap (§8.6) is closed for any stream that sends `start`**: the webhook now mints an HMAC token per admitted call and the relay refuses a stream that cannot present it, or whose call holds no slot — a tokenless `wss://` connection that sends `start` is closed with **1008** and releases nothing. **Corrected seventh pass (§8.31, fixed the same day): the gate was bound to the `start` event, not to the frame path.** `self.refused` is set only inside `_on_start` (`media_stream.py:84,151`), and `_on_media` guarded on it (`:206`) — so a client that opened the socket and sent `media` frames *without* ever sending `start` reached `feed_audio` with `call_id == ""`. `_on_media` now refuses with `media before start` (the receive loop closes the socket 1008 and releases nothing), which is a protocol rule as well as a security one: without `start` the engine has no call identity. Twilio always sends `start` first, so nothing legitimate is refused. `scripts/carrier_acceptance.py` drives Twilio's half of `T-01…T-17` without a carrier; the handset half is listed in `platform/docs/twilio-voice-setup.md` |
| US-002 Web chat, WhatsApp and a configurable extra channel | `COMPLETED` | **Built out 2026-10-04.** `POST /twilio/whatsapp` is served and driven end to end: signature → attribution → `MessageSid` dedupe (ten deliveries → one reply, one turn) → content taxonomy (voice note declined with a text prompt, unsupported files explained, media-without-caption asked for context) → the same answer engine as web chat, grouped on the sender's number → rendered by the originating adapter → `outbound_send`. The outbox implements TRD-04: **5 attempts** with a never-deleted `FAILED` row, transient vs permanent classified by the sender, and `FAILED` rows visible at `GET /api/admin/sends` + the console's sends panel. **Corrected in Batch 7 (§8.26):** five attempts with four waits — 5 s → 30 s → 2 m → 8 m — and `BACKOFF_CEILING_SECONDS = 900` as the documented 5 s → 15 min ceiling, defined but never scheduled. The code, the tests and MOD-01's wording now agree. `ChannelGateway.accept()` now has runtime callers. The extra channel (UC-14) is a config change: `AdapterRegistry` declares→validates→probes→enables idempotently, a dead provider disables *only* that adapter, HMAC is checked over the raw body, and `POST /channels/{channel_id}/inbound` answers it. **Fixed in Batch 7 (§8.27):** `assembly` now wires `credential_probe`, so "declares → validates → probes → enables" runs its third step and a channel declared without its credential refuses to enable at boot. The shipped config still declares no channel — every inbound 404s until one is configured, which is what `system_check` reports as `NOT CONFIGURED`. `system_check` drives UC-02 (answered and queued) and reports UC-14 as NOT CONFIGURED — no extra channel is enabled on this instance. Residuals: the answer is composed on the webhook thread (measured ~0.3 s on the deterministic path; TAC-2's 150 ms ack assumes the story's async split, which the outbox makes possible but does not yet do), and WhatsApp media is recorded, not fetched into the corpus |
| US-003 Browser-mic behind a hardened public edge | `COMPLETED` | **Built 2026-10-04.** `WS /voice/browser` is served (`server.py`): the token arrives in the **first frame** (never the URL — TAC-8), is verified against a signed, expiring HMAC token before any audio is read, and the socket closes with the story's own codes — `4001` token, `4002` admission, `4008` rate limit. Admission runs against the *same* controller as PSTN (`TAC-7`), and a refused session opens no media pipeline. The ticket is a signed token now, and it **forecasts without reserving** (`AdmissionController.peek`): a buyer who denies the microphone after minting one costs no capacity — the WebSocket handshake is the commitment. 30 s idle close with `close_reason='idle'`, session rows carry `close_reason`, `rejected_ingress` counts refusals by class, `rate_limit_event` stores a salted hash. Verified live: both e2e scenarios pass on desktop + mobile against a fresh instance, and the store shows a `browser_mic` session opened and closed `client_stop`. Residual: with `ANITA_SESSION_SECRET` unset the signing key is per-process — a token minted before a restart stops verifying (boot says so; set the key for the reconnect window to survive a deploy) |

### 3.2 MOD-02 — Conversation Orchestrator

| Story | Status | Comment |
|---|---|---|
| US-004 Cross-channel conversation identity | `COMPLETED` | **Built 2026-10-04.** Identity now resolves with the story's precedence — phone > handle > session > anonymous — and normalises at the boundary: `98200 11122`, `+91 98200 11122` and `0091-9820011122` are one key (the +91 bridge is what makes the story's own AC-1 possible, and it is documented as the one assumption). The registry survives its own restart via the database fallback inside the idle window (`AC-2`, `AC-6`), a stale thread yields a **child** conversation carrying `parent_conversation_id` (`AC-4`), ten duplicate starts return one conversation (`AC-5`), and an anonymous session never merges (`AC-3`). `identity_link` is unique on `(type, value)` so two channels linking one number converge by constraint (`TAC-4`); the voice path finally forwards the caller's number (`media_stream.py` read it and dropped it), and lead capture writes the phone the buyer *supplied* onto the conversation so the orchestrator links it. **Verified live:** chat price question → lead captured with a number → a voice turn from that number answered *"The minimum order quantity for Ultrasound Gel is 10 Pieces"* with the product resolved from the web-chat context, and an unknown product still declines. Residual: conversation rows are per identity, and a session-keyed chat thread joins a phone-keyed call thread through the **lead** plus seeded context rather than being one row — the LLD's "three conversations linked" model, chosen over BRD-01's single-thread reading because the numbers that exist mid-flight (a per-tab session id) cannot merge without one |
| US-005 Question classification and routing | `COMPLETED` | `orchestrator.classify()` rules + fail-safe; commercial → SQL, catalogue → retrieval, clinical refused; live on chat and voice. US-006 persists the classification and route on every turn now, closing the "recorded only in memory" gap. **One AC gap found and fixed while wiring US-004:** a follow-up fact question that named no product ("and the price?") declined, because product resolution read only the current message — US-002's AC-2 was never actually implemented. Fixed with a guarded fallback to the product the conversation was already about (`answer_engine._follow_up_product`), which declines as before when the message names something unrecognised. Noted gaps: no LLM-assist fallback for the classifier |
| US-006 Turn serialisation and transcript persistence | `COMPLETED` | **Built 2026-10-04.** `conversation_store.py` owns `conversation`, `identity_link` and `turn`; `seq` is assigned `MAX(seq)+1` inside `BEGIN IMMEDIATE` with `UNIQUE (conversation_id, seq)` and `UNIQUE (provider_event_id)` enforced by the database (`TAC-2`). The whole turn now runs under a per-conversation single slot (`ConversationQueue`, depth 20 from config) — the audit's `[user A, user B, assistant B, assistant A]` reproduction is a test that fails without it — with overflow re-prompting rather than dropping. A store failure buffers into a bounded `ReplayQueue` (capacity from config, drop-oldest counted) and the buyer still gets the answer (`AC-4`); the `ConversationJanitor` sweeps idle conversations, writes idempotent summaries (`TAC-5`, guard on `summary_updated_at`) and drains replay. The review read model pages at 100 with a cursor (`AC-6`), distinguishes `present` / `empty` / `expired` and labels an expired transcript with its date (`AC-7`), and the console renders both, with handoffs and the summary. **Verified live:** three chat turns persisted with classification and route per turn; **48-turn transcript with contiguous seq 1…48 read back through a restarted instance**; `system_check` reads 11+ persisted conversations. **Closed in Batch 7 (§8.25, §8.28):** the queue now covers the voice leg too — the pipeline resolves the conversation and runs the turn through `submit_turn`, reproduced as a six-thread race that comes out `[user, assistant] × 6` — and the janitor gained two duties: retention (`expire_transcript` + a new due-list query, driven by `retention_days`, 0 disables) and summary retry (`unsummarised_closed`), so the review's `expired` state and the "retry a lost summary" path both exist in production. `conversations_for_identity` was removed as unused. Residual: SQLite shares one connection between reads and writes, so `TAC-7`'s "separate pool" is not claimed (recorded in the module docstring) |

### 3.3 MOD-03 — Retrieval & Grounding

| Story | Status | Comment |
|---|---|---|
| US-010 ANITA-only index build and validate | `COMPLETED` | **Re-promoted 2026-10-04 after the staging-collection defect (§8.23) was fixed.** The lifecycle, the store and the pointer were already real — `scripts/rebuild_index.py` drives begin → embed → validate → promote-or-reject, `SqliteBuildStore` persists the builds with the story's **partial unique index**, promotion is one transaction, the serving process reads the LIVE collection as a pointer (`assembly.py:161-179`), boot **refuses** with no index and no promoted build, and a failed gate leaves the previous build serving. **The fix:** `index.staging_retriever()` binds the embed to the collection the build *names* (`index.py`), `SharedRagRetriever.for_collection()` supplies the sibling (`rag_adapter.py`), and the gate validator now points `chat.retriever` at that same sibling for the run and restores it afterwards, including on failure (`rebuild_index.py::_gate_validator`). **Verified end to end on the real stores:** a rebuild promoted `anita_ams__v5` with **141 vectors embedded and validated**; with no serving process running, v5 already held its 141 vectors immediately after promotion (under the old code it would have been empty and lazily filled, unvalidated); the build row records `all 70 cases passed` against set `eb2ffc27…`; and a fresh process logs `index pointer: serving promoted collection anita_ams__v5` with `store.count() == 141`. **Tests:** the reproduction fails without the fix — with the retarget removed, the second rebuild embeds into `v1` instead of `v2` (`test_the_embed_targets_the_collection_the_build_names`), plus `test_a_staging_build_does_not_write_the_live_collection` and the validator-restore test. Residuals: the column type is SQLite dialect (`CR-02` moves it); the collection name is not surfaced on the admin console; `INDEX_BUILDS_DDL` (Postgres) is referenced only by a test; and a promotion takes effect for a running process only **on its next construction** — `assembly` reads the pointer once at boot, so a live process keeps serving the collection it opened until restarted (the fifth pass claimed "every process at the moment it commits", which was never true) |
| US-011 Hybrid retrieval with a grounding gate | `COMPLETED` | `MiniBM25` + weighted RRF 0.65/0.35 + `GroundingGate`, live by default (`rag.backend: shared`); `ANITA_RAG_BACKEND=memory` rollback proven (the env var is read in `assembly._rag_backend`, `assembly.py:100-112` — not in `rag_adapter.py`, as the fifth pass said). Minor (re-confirmed seventh pass, refs corrected): fusion weights are hardcoded (`rag_adapter.py:235-236` — `dense_weight=0.65, sparse_weight=0.35, rrf_k=60`; the sixth pass's `:202-203` was wrong), `MiniBM25` lives in `retrieval.py:145` and `shared/rag/fusion.py:34` rather than in the adapter, and the shared service's `degraded` flag is dropped when the adapter rebuilds the result into ANITA's `RetrievalResult` (`rag_adapter.py:141-153`) — it survives only in `status()`, so no turn can tell that retrieval went sparse-only |
| US-012 Provenance and honest absence | `COMPLETED` | **Built 2026-10-04.** `coverage.py` is the single definition of "published", keyed **per product id**: it returns exactly `{270685, 270687, 381893}` with no specs and `{270683, 3935251, 3935247, 3935249}` with no About, asserted against the live corpus, and derived rather than cached so a refreshed corpus flips it with no code change. The engine consults it **before retrieval** — so a product the catalogue stocks but has not documented is answered *"ANITA has not published specifications for Microbar Paste yet…"* with a human offer and **0 LLM calls** (`TAC-4`), instead of the old *"I don't have that in our catalogue"*, which told buyers we do not sell what we do sell. Every grounded reply now carries `grounded_on` (chunk id, type, product, source URL, content hash — the existing TRD-16 payload, finally surfaced); the content hash and ids are stored on the turn so the console review renders the grounding per turn. `citation_incomplete` is counted, and the release gate now runs the frozen set a second time counting citations: **55 grounded answers, 0 uncited, 0 incomplete** — it found and fixed two real defects on the way (the listing answers were uncited; a record citation without a URL was being misflagged). **Corpus-vs-story note:** the story's edge 11 says one "Medical Dry Imager" has About and the other does not — against the live corpus **both** lack About; the test asserts the true property (per-id independence) and records the discrepancy rather than the story's version. Residuals (sixth pass): the probe is not yet exposed on the admin console; named-spec-level emptiness (a spec row with a blank value) is handled by the corpus lookup, not by the probe; the **turn store keeps only a reduced citation** — `surfaces.py:250-262` persists `chunk_id or source_url` strings (`grounding_refs`), so the chunk type, product and content hash the reply carries (and the console renders) are lost from the reviewable record; and **the citation check now enforces** (§8.24, Batch 7): `combine_validation` takes the counts, refuses promotion on any uncited or incomplete answer (or a run that did not complete), and the counts travel into the JSON report — verified live as `{'grounded': 55, 'uncited': 0, 'incomplete': 0}` |

### 3.4 MOD-04 — Inference & Answer Composition

| Story | Status | Comment |
|---|---|---|
| US-013 Deterministic price / MOQ / delivery with caveat | `COMPLETED` | `answers.py` — fact registry, `ProductResolver` (Resolved/Ambiguous/NotFound), caveat appended in the same function that produces the figure; live on chat and voice, with no LLM on the path. Noted gap: the `corpus.caveat_violations()` boot guard exists but is **never called at boot** |
| US-014 Grounded prose, clause-streamed for voice | `COMPLETED` | **Done 2026-10-04 — wired per the recorded decision (deterministic-first; prose only for open questions).** The backend is now **constructed at assembly** (`OllamaBackend` from the configured model, with a breaker) and injected into the one shared answer engine, so the prose leg and the breaker are no longer a library island. `AnswerEngine._compose_prose` is tried at **exactly one place** — the fall-through where an open question would otherwise be answered with the top chunk verbatim — and nowhere else: price, MOQ, delivery, spec, company facts, refusals and declines all answer before it is reached. Generated text is checked by `prompts.verify_answer` against the same context the prompt carried (numeric grounding + the BRD-06 caveat), so a fabricated figure or a price without its caveat is rejected and the verbatim answer stands. **Evidence:** `pytest tests -q` → **948 passed** (9 new: grounded accepted, fabricated figure rejected, missing caveat rejected, dead model falls back, verbatim answer survives, the fall-through uses prose, a decline never reaches the model); **live** against the real Ollama and the real corpus — *"what is the ultrasound gel used for?"* → 7.8 s, `accepted=True`, breaker closed, prose naming the gel's use. **Residuals:** the configured model (`qwen2.5:14b`) does not answer inside the backend's 60 s timeout on this CPU-only host, so the live demo used `llama3.2:3b`; the prose is not yet covered by the frozen evaluation set (US-027's set predates the leg) |
| US-015 Keep selling when the model is down | `COMPLETED` | **Done 2026-10-04.** With the backend live, the breaker actually runs: every prose failure — breaker open, timeout, transport error, empty generation, grounding rejection — is swallowed in `_compose_prose` and the caller keeps the deterministic or verbatim answer, so a dead model degrades **prose and nothing else**; the commercial path never touches the model at all (asserted: zero model calls during a price question, live). Breaker state is now on every turn's record (`llm_configured` / `llm_available` in the chat trace), so "the answer was verbatim" can be told apart from "the model is down" after the fact. **Defect found live while wiring this:** `OllamaBackend`'s default `keep_alive="-1"` is rejected by Ollama with HTTP 400 (`time: missing unit in duration`) on *every* request — a default-only trap that reads as a model fault; the default is now `-1m` (the configured value always was) with a test pinning the unit. **Residuals:** no console metric for the breaker (the trace carries it); no provider probe or first-token retry beyond the breaker's cooldown. Precision (sixth pass): `llm_configured`/`llm_available` are written to the **per-turn trace** (`surfaces.py:301-308` → `perf_turns.jsonl`) — verified present on 2 580 live records — not onto the conversation-store turn row, so "was the model down?" is answerable from the trace stream but not from the review API |

### 3.5 MOD-05 — Speech Services

| Story | Status | Comment |
|---|---|---|
| US-016 STT: 20 ms frames, VAD, 600 ms floor | `COMPLETED` | **Promoted from `IN PROGRESS`.** The media-stream relay reaches the real STT leg — the accurate chain, **re-traced seventh pass**, is `media_stream.py:263` (`feed_audio`) → `:264` (`_begin_turn`) → `:290` (`process_utterance` in `_run_turn`) → `voice.py:1297` (`_transcribe`, with the speculative pass short-circuiting at `:1280-1294`) — and the simulated call transcribed `"How much is the ultrasound gel?"` with faster-whisper on CPU. The sixth pass's citations (`media_stream.py:218`, `voice.py:1278`) were again off: `:217-219` is the TAC-6 `call_expired` check and `voice.py:1278` is a comment. Noted: `speech.EndOfSpeechTracker` is a parallel implementation the live engine does not use (`voice.py` counts silence itself); it survives only in tests and the diagnostic `scripts/voice_e2e.py`. The fixed-response refusal path is silent only while no asset is configured; the `note_asset_played` arity defect that would have raised the moment one was is fixed (§8.10, Batch 7), so the branch records a *degraded* outcome rather than a failed one when it runs |
| US-017 Clause TTS with immediate barge-in stop | `COMPLETED` | **Demoted 2026-10-10 (seventh pass), fixed the same day (§8.30).** The mechanism was built and tested; the *trigger* was missing — `voice.py:776 mark_speaking()` had no caller anywhere in the repository, so `admit_inbound_frame` returned `FRAME_LISTENING` unconditionally (`:945`) and the gate could never return `FRAME_INTERRUPTION`. **The fix, now live on both transports:** `send_frames` calls `mark_speaking()` on the turn's **first** outbound batch — the moment the carrier (or the page's queue) starts playing us out — and `note_playback()` on every batch after it; `_run_turn`'s `finally` calls `end_turn()` on every exit path. On the engine side `end_turn()` no longer clears `_turn_began_as_interruption` (the transport's teardown runs *before* the next utterance reads it), and the flag is now consumed exactly once where it decides the min-burst floor — which also closed a latent leak, since it had no live clearer at all and one interruption would have lowered the noise floor for the rest of the call. Everything downstream is unchanged and now reachable: the clause loop stops per clause, `_synthesise_streamed`/the TTS producer stop mid-clause, a partial is never cached, `clear_playout` fires on the transition into an interruption, and the transcript is marked truncated. **Evidence:** `pytest tests -q` → **1176 passed, 1 skipped** (13 new: 5 on the relay's window, 4 on the session's flag lifecycle, 3 on the frame-path gate, 1 on the browser relay); `system_check.py` → **20 · 0 · 0 · 1 deferred**. Every reproduction was probed by reverting each half of the fix and watching the new tests fail — the end-to-end one drives a **real `VoiceCallSession`** through the relay with neither the verdict nor `_speaking` stubbed, which is how the existing tests missed it. **Residuals:** AC-1's ≤300 ms first-audio and AC-2's ≤150 ms stop-to-silence budgets are engine-side only until the real carrier call, and the gate's *thresholds* (`BARGE_IN_ABS_FLOOR`, the echo ratio) are tuned for a real handset and echo path — the real call is where they get confirmed; the `clear` round trip to Twilio is unmeasured |
| US-018 VRAM admission, degrade voice before text | `COMPLETED` | **Done 2026-10-04.** Three gaps closed: (1) **the ladder now moves** — `anita/capacity.py` samples device free memory (nvidia-smi; `None` means *not measurable* and the policy then does nothing rather than guessing), publishes `gpu_free_mib` to the metrics registry (previously registered unavailable and never written), and walks the ladder down when the **sibling's floor** is threatened and back up only with headroom (`HYSTERESIS_MIB=512`, so a box at the threshold does not flap). Order is the story's: `voice_shed → stt_off → tts_off → chat_degrade → commercial_only`, and a shed rung refuses the next call while text stays available. (2) **The configured capacity numbers reach the controller** — `vram.anita_budget_mib` / `shared_card_mib` / `sibling_floor_mib` were read by nothing and the controller ran on dataclass defaults; now they are passed (TAC-7's "an unread capacity key is decoration"). (3) **The boot claim is true** — a host without a CUDA device is **capped at 1 concurrent call** (`CPU_CONCURRENT_CALLS`; CPU synthesis is ~0.6× real time), not "refused at admission" as the report used to claim with no code behind it; voice still works, which the simulated call proved. **Update 2026-10-04:** the cap was a dependency artefact on this box, not a hardware fact — with CUDA torch installed the host is `voice_capable`, the ceiling is the configured **2**, and the ladder's monitor now samples a real device (§9, "The GPU, enabled 2026-10-04"). The monitor is started only where there is a device to read — on CPU there is no VRAM to shed, and a monitor would be theatre. **Evidence:** `pytest tests -q` → **938 passed** (11 new: the ladder order, voice-first shedding, refusal on a shed rung, recovery with hysteresis, unmeasurable-is-not-pressure, metric publishing, config-to-controller, the CPU cap, and boot-report-matches-behaviour). **Residuals:** the sampler's 5 s cadence and the 30-minute soak (TAC-2) need a GPU host — no CUDA device exists on this machine, so the pressure path is proven with a forced source, not with real memory pressure; the capacity trace (TAC-5's "visible in the trace") is the `gpu_free_mib` metric plus the logger, not a durable capacity-events table |

### 3.6 MOD-06 — Lead & Mock CRM

| Story | Status | Comment |
|---|---|---|
| US-019 Lead capture and dedupe | `COMPLETED` | `leads.py`; dedupe by phone/email backed by unique partial indexes in the store. Noted: only `web_chat` is ever passed as the channel label |
| US-020 Mock CRM behind a swappable interface | `COMPLETED` | `crm.py` — `CrmStore` Protocol, SQLite implementation, outbox. The seam is duck-typing only; no second implementation and no config-driven selection (`assembly.py:499-504` builds `MockCrmStore` unconditionally). The CRM-side outbox seam (`enqueue`/`pending_outbox`/`mark_sent`/`mark_failed`) and its `crm_outbox` table were **removed in Batch 7 (§8.28)**: a second queue that nothing produced or consumed, while the live outbound queue is the *messaging* `SendOutbox` in `outbox.py`. The `CrmStore` Protocol still covers what the product uses |
| US-021 Outbound queue and dialling worker | `BLOCKED` | `OutboundQueue` **is built** — window and attempt limit checked at claim time, `SELECT … FOR UPDATE SKIP LOCKED` semantics, abandoned-row reaping — but it is instantiated **only in tests** (`tests/test_mod06_leads.py`). No worker, no dialler and no route to trigger one exist anywhere in the repo. Held deliberately: placing calls to real buyers needs the DG-08 compliance review to close first. UC-09 is unreachable. **Location note (sixth pass):** the dialling queue lives in `leads.py:323-467`; `outbox.py` is the *messaging* send queue (`SendOutbox`/`OutboxWorker`), which **is** live — two different queues that the fifth-pass wording could blur |
| US-022 Handoff records and rep notification | `COMPLETED` | **Done 2026-10-04.** The audit's three defects, all fixed: (1) `crm.record_handoff` **assigns the id** when the caller passed none — the empty string used to be inserted verbatim, so the first handoff took the empty PRIMARY KEY and every later one died on the UNIQUE constraint, swallowed into `dispatched=False` (**one handoff per database lifetime**); (2) the handoff carries a real `conversation_id` on its own field (guarded `ALTER TABLE` migration for existing DBs) and `surfaces.py` passes the true `lead_id` — empty when the buyer gave no contact details, never a conversation id masquerading as a lead; anonymous escalations dedupe per conversation, known leads per lead; (3) **notification exists**: `anita/notify.py` emails the rep over SMTP (`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SALES_NOTIFY_EMAIL`, DG-12's email default), a delivered notification moves the handoff to `state='notified'` with `notified_at`, and a missing or broken notifier leaves it **open** without failing the turn — `EscalationRecord.notified` keeps "we escalated" and "somebody knows" as separate facts. (4) `GET /api/admin/handoffs` behind the bearer boundary serves the queue, with an **Escalations** panel on the console page showing the open count. **Evidence:** `pytest tests -q` → **927 passed** (10 new, incl. two handoffs for two leads — impossible before this change); `system_check` → 16 passed · 0 failed · 5 deferred. **Residual:** with no SMTP configured the boot report says so and handoffs stay `open` — honest, but a deployment must set the keys for the rep to be reached |

### 3.7 MOD-07 — Sentiment & Escalation

| Story | Status | Comment |
|---|---|---|
| US-023 Per-turn sentiment scoring | `COMPLETED` | Rule-based fallback always works — preserved through the CR-01 merge. Noted: scoring is **chat-only**; voice turns are never scored |
| US-024 Composite, category and same-turn escalation | `COMPLETED` | Escalation reaches a real sink wired in assembly; idempotent per lead |

### 3.8 MOD-08 — Observability & Evaluation

| Story | Status | Comment |
|---|---|---|
| US-025 Dependency-free per-turn trace | `COMPLETED` | **Done 2026-10-04.** `new_trace(call_id, turn)` accepts the engine's positional call (`voice_trace.py`); `TurnTrace` gains `mark()`/`note()`/`stages_seen`/`durations_ms` — absent-not-zero (TAC-3), duplicate marks counted, non-numeric counters refused (the PII guard, TAC-4), backwards clocks clamped and flagged; the relay emits on every turn outcome (`media_stream.py`) and chat emits `turn_received → answer_composed → sentiment_scored → outcome` (`surfaces.py`); `JsonlSink` rotates 100 MB × 7 (TAC-5) and a failing sink is counted, never raised (TAC-1). `assembly.py` now passes a real `JsonlSink` — it had passed a bare `Path`, so every emit silently `AttributeError`-ed — and installs the writer into the voice seam. **Evidence:** 11 new tests incl. a driven end-of-speech trace and the stdlib-only import guard; `pytest tests -q` → **886 passed**. **Residual:** emission is one synchronous append, measured at **0.36 ms/turn** — inside the ≤2 ms budget (TAC-2) but not the literal "zero blocking I/O"; a background writer is the remaining refinement |
| US-026 Admin console read API | `COMPLETED` | Endpoints served: `GET /health`, `GET /api/admin/{channels,conversations,conversation,metrics,handoffs,sends}` and `POST /api/admin/gate` (`surfaces.py:44-63`, served by the `server.py:127-134` route loop). **Precision (sixth pass):** "all behind the bearer boundary" is wrong for **`GET /health`, which is deliberately open** (`ROUTE_TABLE` marks it `needs_auth=False`); every other admin route is bearer-authenticated (`surfaces.py:72-78`, `Router.dispatch`). "Conversation review returns no sentiment" is half-true: the **list** endpoint hardcodes `sentiment_category=None` (`surfaces.py:558`), the **detail** endpoint reads sentiment from the in-memory registry (`:615,655-668`) — so a live conversation shows it and a restarted or purged one does not, and no sentiment is ever persisted. **Traces are exposed to no handler** — that gap is real for the story, and the writer that `AdminSurface` held and never read was removed in Batch 7 (§8.28) rather than left suggesting an endpoint exists |
| US-027 Frozen eval set and release quality gate | `COMPLETED` | **Done 2026-10-04.** The set is now a versioned repo file — `eval/anita_frozen_set.jsonl`, 70 cases, SHA-256 pinned in `config/anita.yaml` (`release.eval_set_sha256`) — and `assembly._build_release_gate` loads it through `load_eval_set()`, **fails closed** on a missing file, an empty set or a hash mismatch, and stamps the digest on every report (TAC-1). `cases_from_corpus()` no longer runs at boot. Regeneration is a reviewed act: `scripts/freeze_eval_set.py` shows the added/removed case ids on a dry run and only writes with `--write`. The `QualityGate` false-green is fixed: the verdict is derived, and a tolerated non-critical failure is named in `reason` instead of being reported as "all N cases passed". The missing operational entry point exists: `scripts/rebuild_index.py` drives `IndexLifecycle` through `begin → embed → validate(frozen set) → promote/reject`, so a rebuild is gated and rejection leaves the live index serving (`--dry-run` validates what is live). **Evidence:** `pytest tests -q` → **896 passed** (10 new); `release_gate.py --no-browser` → 70/70 PASS from the frozen set; `rebuild_index.py --dry-run` → `PASS — all 70 cases passed`, exit 0. **Residuals:** the story's 90-case figure is aspirational (the frozen set is the 70 the gate always ran); VOID is not a distinct verdict — an unreachable engine fails every case and blocks via the critical rule (TAC-3 holds); the boot path still builds the dense index lazily through `rag_adapter` without versioned collections, so the validated promotion is an explicit operator action rather than the boot path |

### 3.9 MOD-09 — Configuration, Isolation & Boot

| Story | Status | Comment |
|---|---|---|
| US-028 Typed config, boot refuses unread keys | `COMPLETED` | Catches keys set but never read; typed parsers never fall back silently. Declared-but-unread keys are `PENDING` by design, not refused |
| US-029 Isolation guard, refuse to boot | `COMPLETED` | Verified live: a copy-pasted sibling config (`ANITA_DB_NAME=admissions`, ports 8000/8501) is refused with exit code 2, naming the resource, both values and the fix |
| US-030 Platform detection, secrets, preflight | `COMPLETED` | Secrets masked in every render; a refused boot leaves no artifact behind. Noted: `voice_capable=False` is reported but, as of US-018, does not actually refuse voice at admission |

### 3.10 MOD-10 — Client Surfaces

| Story | Status | Comment |
|---|---|---|
| US-007 Responsive web chat surface | `COMPLETED` | Served at `GET /`; 86 markup/surface tests pass. **Qualification:** the "50/50 browser scenarios" evidence drives the **Streamlit** surface (`streamlit_app.py`, Streamlit selectors in `tests/e2e/scenarios.spec.js`), not `app/static/chat.html`, which has markup-level tests only |
| US-008 Browser voice client, typing one tap away | `COMPLETED` | **Built 2026-10-04.** `chat.html` no longer records-and-discards: capture is the AudioWorklet (`static/pcm-worklet.js`, PCM16 @ 16 kHz — the frozen client contract), with a MediaRecorder fallback that sends the utterance then `commit`s the turn. Playback is one ordered queue of µ-law-decoded frames; `stop_speaking`/`flush` clears it. Every voice state is also a sentence in a `role="status"` region; permission-denied, no-device, busy, dropped and rate-limited all land in DEGRADED with the text chat usable and the callback number shown when the server supplied one. No storage is written (TAC-5), the token never travels in a URL, and reconnect is 1 s → 8 s within the 60 s window. The frame/close-code contract is frozen in `MOD-10` §B.3 and asserted by `test_static_surfaces.py` (contract) + `tests/e2e/voice.spec.js` (live, desktop + mobile) |
| US-009 Admin console behind a login | `COMPLETED` | Bearer-token admin surfaces (UC-12, UC-13, UC-15); auth fails closed on an unset token. Noted: the story asked for a signed session cookie; the implementation is a bearer token held in `sessionStorage` |

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
| B3 | **`import av` is blocked by Smart App Control** (`VerifiedAndReputablePolicyState = 1`) — 3 of PyAV 19.0.1's 67 extension modules. The FFmpeg DLLs are unaffected | ✅ **Resolved — PyAV 18, not a machine change.** The block was version-specific: 19.0.1 is refused, 18.0.0 imports across five releases measured in a throwaway venv. `av>=18,<19` is pinned with the finding in `requirements.txt`. Smart App Control stays on. The earlier "disable SAC or stub `av`" framing was wrong (commit `382622e`) |

**Note on B2:** the `scipy` block was *transient*. Smart App Control blocks freshly-written binaries
until they acquire reputation, so `scipy` failed minutes after `pip install` and passed later
unchallenged. Worth knowing before diagnosing a future post-install import failure as a real problem.

**STT is no longer blocked.** With `av` importable, the real chain was driven through the engine —
`feed_audio` → VAD → `process_utterance` → `_transcribe` with real faster-whisper weights and the
µ-law codec: `"how much is the ultrasound gel"` → `'Caller: How much is the ultrasound gel?'`,
100 % word overlap. The `TestTheAudioScaleContract` guard added with that fix exists because an
int16/normalised mismatch fails as a *hallucination*, not an exception.

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
| C8 | `ChannelGateway.public_base_url` is never set by `assembly.py`, so signature validation would compare against `""` | ✅ **Fixed — confirmed seventh pass.** `assembly.py:383-393` derives it from `TUNNEL_HOST`, `ChannelGateway.__init__` stores it (`channels.py:307,315`) and uses it to build the signed URL (`:358`). The marker here was stale; §3a records the fix |

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

**Verified:** with `TUNNEL_HOST` now also set, the boot preflight reports **zero** degradations.
`TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` (`+12792365960`) and `ANITA_ADMIN_TOKEN` all load — see
§7.

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
| `clear` can be sent to stop Twilio's buffered playout | ✅ Since Batch 7 `clear_playout()` **has** a caller — the relay sends `clear` on the transition into an interruption (`media_stream.py:249`, def `:349`) — and since §8.30 that transition is **reachable**, because `send_frames` arms the engine's SPEAKING window on the turn's first batch |
| `stop`, and an **abnormal drop**, both release the admission slot | AC-8 / SM-04 — a leaked slot makes the box refuse calls while idle |
| `close` is idempotent and never releases another call's slot | |
| No audio is sent after close; late frames are ignored | frames in flight during teardown |
| The call id is masked in logs | TAC-8 / REC-14 |

Two defects were found **by these tests**, not by review: `_on_media` fed frames arriving after
teardown, and a close could have released a slot twice. One test bug of my own was also corrected —
it admitted two calls into a controller whose ceiling was 1.

### The channel tables (`channel_store.py`, 2026-10-04)

Three tables, each earning its place by making a specific failure impossible rather than unlikely:

| Table | The failure it removes |
|---|---|
| `inbound_event` | **Replay.** Twilio retries a webhook it believes failed; a replayed `CallSid` must not open a second media stream or start a second conversation. The provider's own event id is the primary key, so the guarantee is the database's — the same reasoning as the unique partial indexes on leads |
| `channel_endpoint` | **Attribution.** A valid signature proves Twilio sent it, not that this instance owns the number. Answering for a number we do not own would make this product speak for the other one (`CG-1`, `BRD-03`, edge case 14) |
| `channel_session` | **Identity.** Keyed on the stream SID, so a caller who withheld their number still has one stable identity for the call (`AC-6`) |

**SQLite, not Postgres, and deliberately so.** US-001's LLD gives this DDL in Postgres dialect
(`uuid`, `jsonb`, `timestamptz`, `gen_random_uuid()`), but CR-02 owns that swap and is blocked on
Q9–Q12, with the recorded constraint that changing the persistence layer and the modules at once
makes every regression ambiguous. These are the same tables in the dialect the platform runs, behind
the same seam the `CrmStore` Protocol established — so CR-02 moves them rather than rewriting them.

**Two divergences from the LLD, recorded rather than absorbed:**

* `endpoint_id` is **nullable**. The story has it `NOT NULL REFERENCES channel_endpoint(id)`, but an
  event from an unrecognised number is exactly the one that must be *recorded as rejected*, and it
  has no endpoint to point at. A placeholder row would put a fictional endpoint in the table.
* `channel_kind` uses the `Channel` enum's `voice_inbound`, not the story's `inbound_call` — the
  value the conversation actually carries, and consistency within the code is worth more than
  agreement with prose.

**Verified:** `+12792365960` is registered at boot against instance `anita` with
`secret_ref = TWILIO_AUTH_TOKEN` (the *name*, never the value — `REC-21`). A 20-thread barrier race
on the same `CallSid` produces **exactly one** `accepted`; replay protection survives a reopen of
the real database file; a foreign number is refused **and** recorded as `rejected` without consuming
capacity; and a broken store does not drop a call in progress.

### The simulated call — the whole path, proven without a carrier (2026-10-04)

`logs/instance.log.err` records a complete call over the **public tunnel**, driven by a simulated
Twilio client (`CA-simcall-1`):

```
WebSocket /twilio/media-stream [accepted] → media stream: started
  80 frames → USER_SPEECH_STOPPED (600 ms floor)
  faster-whisper small.en on cpu → STT: "How much is the ultrasound gel?" (prob 1.00)
  LLM/TTS: "Ultrasound Gel is listed at ₹60/Piece. This is the published list price, excluding
            taxes; a formal quotation is required for an order."
  media stream: closed (disconnect, 1 turn(s) transcribed)
```

A `POST /twilio/voice` through the tunnel returned **200** with valid signature, and the channel
store holds the expected simulated identifiers (`CA-public-test-1`, `CA-verify-2c1b8167`,
`MZ-simcall-1`) with `+12792365960` registered as an owned endpoint. So signature validation,
attribution, admission-before-TwiML, the media relay, VAD, STT, the answer engine, clause TTS and
teardown are all exercised on the real code path.

### Still to build for US-001

1. **A real carrier call.** No real phone call has ever been placed — every CallSid on record is
   simulated (`CA-simcall-1`, `CA-public-test-1`, …). **Seventh pass, verified exhaustively:** a
   read-only scan of every SQLite database under `platform/logs` and `platform/data`, plus a
   byte-level grep over the logs and the Chroma store, finds **no real 34-character Twilio CallSid
   anywhere**. `inbound_event` holds 66 rows, all `disposition='accepted'` against the single
   registered endpoint `+12792365960`; 61 of them are the synthetic `CA` + zeros + hex shape
   `scripts/carrier_acceptance.py:89-97` mints. `T-01…T-17` are outstanding, including the `T-17`
   three-call load test against the 2-slot admission ceiling.
2. ~~**The media-stream WebSocket enforces no admission of its own.**~~ ✅ **Fixed 2026-10-04
   (§8.6), completed 2026-10-10 (§8.31).** The webhook mints an HMAC token for the admitted call
   and stamps it into the TwiML; the relay verifies it and requires the call to hold a slot before
   feeding a frame, refuses otherwise with close code **1008**, and releases nothing on a refused
   socket. Reproductions in `tests/test_mod01_media_stream.py::TestTheStreamIsAdmitted` fail without
   the gate. **The seventh pass found the gate guarded the `start` event only** — a stream sending
   `media` without `start` reached the engine unchecked — and `_on_media` now refuses
   `media before start`. The invariant holds for every stream, announced or not.
3. **Public ingress is operational, not fixed.** `start_services.ps1` and
   `scripts/update_twilio_webhook.py` automate the tunnel → `TUNNEL_HOST` → webhook re-point
   sequence, and `TUNNEL_HOST` is set. But a *quick* tunnel mints a new hostname on every restart;
   a named tunnel or a domain would remove the class of problem. `docs/twilio-voice-setup.md` is
   the runbook — note its "What is still missing" section is stale (it says TAC-6/AC-7 are not
   implemented; they are).

### TAC-6 and AC-7 — implemented 2026-10-04

Both were previously listed here as not implemented. They are now built and tested (17 tests).

**`AC-7` — the unintelligible-utterance ladder.** The ladder already existed but had two defects
that made it fail the criterion:

* It never *ended* the call. The last rung repeated forever, so a caller who could not be heard
  stayed on the line indefinitely holding one of the two admission slots.
* Its third rung offered **WhatsApp or email** — and inbound WhatsApp is a deferral on this
  product (`UC-02`), so the assistant was naming a channel it cannot answer on. The human-transfer
  offer sat at the *fourth* rung, where `AC-7` puts it at the third.

Three rungs now, the third terminal: plain retry, line-quality hint, then a human callback offer
after which the call closes with the transcript retained. A test asserts no rung names a deferred
channel, so the WhatsApp text cannot come back.

**`TAC-6` — the call ceiling.** Nothing existed: no duration tracking at all. A call that never
reached end-of-speech ran until someone hung up.

`MAX_CALL_SECONDS` (default 1800) is declared in `config/anita.yaml`, parsed into `SpeechSettings`,
exposed through the engine's settings shim, and read **per session** like the VAD delay — a value
nobody can reach is not a setting. The check sits at the top of `process_utterance`, *before* the
empty-buffer return, because the case it exists for is a call where there may be no audio at all.
The relay asks `call_expired()` on every inbound frame, since frames are the only thing arriving on
a silent call, and forces the turn so the closing line is spoken and the call tears down through
the normal path rather than a special one. The ceiling is announced once (`mark_capped`), so
subsequent frames cannot re-trigger it.

**One test was rewritten rather than deleted.** `test_the_engine_is_a_faithful_copy` compared the
copied engine's definition list against the source's, which both changes violate. Its intent —
catch an accidental rewrite of experimentally-derived tuning — is still worth keeping, so it now
allows **exactly** the documented divergences and fails on anything else. It caught an entry I had
forgotten to list, which is the guard working.

---

## 4. CR-01 — RAG + Sentiment re-architecture

**10 / 10 stories `COMPLETED`. Integration checkpoints C1–C9 all `PASS`.**
Full per-story evidence lives in `change-requests/STATUS.md`; this is the roll-up.
All ten were independently re-verified on 2026-10-04 — with one live defect found in US-114's sink
(§8.1), which is a runtime bug in delivered work rather than a failure of the extraction.

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
| Sentiment | US-114 Escalation with a real sink | `COMPLETED` | **UC-10 `DEFERRED` → `PASS`**; two wiring defects fixed. ⚠ Re-audit 2026-10-04: the sink opens a `Handoff` but the empty `handoff_id` means **only the first handoff ever succeeds** — see §8.1 |
| Sentiment | US-115 Sentiment store Protocol | `COMPLETED` | Injected, never global; two cores proven independent |

**New code introduced:** `shared/rag` (8 modules) and `shared/sentiment` (5 modules — the fifth pass
said six) — product-agnostic, neither imports `anita.*` (re-verified sixth pass by grep over all 13
files; the only "ANITA" hits are prose in docstrings). ANITA consumes both through
`anita/rag_adapter.py` and the sentiment facade.

**Independently re-verified 2026-10-04, twice (fifth and sixth passes):** both packages exist, the
grep/AST scan over all 13 shared files finds zero `anita.*`/`app.*` imports, the adapter is live by
default and the `memory` rollback is proven. Of the two small defects the fifth pass recorded, one
is **fixed** — `shared/sentiment/core.py` now logs as `shared.sentiment` (§8.18) — and one is
**still open**: `change-requests/STATUS.md` §"store integration matrix" marks all four stores
`NOT STARTED`, contradicting US-103 and US-115, which the same file records as `DONE` (§8.16).

---

## 5. CR-02 — Production infrastructure (Postgres + Docker)

**Status: `BLOCKED` on decisions Q9–Q12.** Not started. Deliberately sequenced *after* CR-01.
Verified 2026-10-04, re-verified sixth pass (unchanged): no `Dockerfile`, compose file or
`Postgres*Store` exists anywhere; `psycopg2-binary` is declared (`requirements.txt:18`) but imported
by nothing; `DB_*`/`DATABASE_URL` appear only as known key *names* in `config_truth.py` and are read
by no code (they are the bulk of the audit's `PENDING` list — see §8.29 for that bucket's limits).

| # | Decision needed | Recommendation on record |
|---|---|---|
| D-1 | PostgreSQL or MySQL? | **PostgreSQL** — `psycopg2-binary` is already a declared dependency (`platform/requirements.txt:18`; the `:20` in this row and in `CR-02-production-infrastructure.md:36` was a stale reference, corrected seventh pass) and the sibling product already runs it. MySQL would add a second engine to the estate for no gain |
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
| **DG-06** Auth, rate limiting, Twilio signature validation | `COMPLETED` (code + config) — live confirmation pending a real call | `validate_twilio_signature()`, `RateLimiter` and admin bearer auth are built and tested. **`TWILIO_AUTH_TOKEN` and `TUNNEL_HOST` are now set in `.env`, and `.env` is actually loaded at boot** (`config.py::_environment`), so both secrets the signature path needs are present — the earlier "TWILIO_AUTH_TOKEN is unset" note is obsolete. **Precision (re-verified seventh pass):** `preflight()` returns **OK with exactly one degradation — SMTP unconfigured** (§7, re-run). The "three degradations" recorded in the fifth and sixth passes (CPU host capped at 1 call · SMTP · `ANITA_SESSION_SECRET`) is obsolete: the GPU is enabled (ceiling back to the configured 2) and `ANITA_SESSION_SECRET` is set. What remains: a real carrier call to confirm the signature path end to end |
| **DG-08** Legal review of clinical-refusal wording | `YET TO START` | A human/legal task, not engineering. Blocks public launch, and blocks UC-09 outbound (calling real buyers) |
| U-1 UC-10 A1 "buyer declines a human" | `YET TO START` | Deferred — needs a decline intent that does not exist in the evidence. `escalation_enabled` is the operator equivalent for now |
| U-2 `MIN_LABELED_OUTCOMES` operator-tunable? | `YET TO START` | Currently a code default of 100 |
| U-3 Side A's exact BANT completeness rule | `YET TO START` | Not present in the evidence |

### Deferred use cases — by decision, not by defect

Each carries a written reason in `config/anita.yaml` → `release.deferred_use_cases`.

**Verified 2026-10-04: exactly one use case is still deferred — `UC-09`.** The UC-02/UC-03/UC-04/UC-14
entries were *removed from the config* rather than rewritten, because the code made their reasons
false and the config says so in a comment at the removal site. `scripts/system_check.py` now
*drives* UC-02, UC-03 and UC-04 (all `PASS`, with UC-14 `NOT CONFIGURED` — no extra channel is
enabled on this instance), so a regression in any of them fails the check instead of hiding behind
a deferral.

| UC | Reason (abbreviated) |
|---|---|
| UC-09 Outbound follow-up | The only remaining deferral. `OutboundQueue` has no worker. Deliberate: calling real buyers needs DG-08 to close first |
| UC-02, UC-03, UC-04, UC-14 | **No longer deferred.** Removed from `config/anita.yaml` on 2026-10-04; each is served and driven by `system_check`. UC-03's remaining item — a real carrier call — is US-001's residual (§3a), not an omitted capability |

---

## 7. Verification — re-run on this machine, 2026-10-10 (seventh pass)

**How to read the table.** Rows marked **re-run** were executed again during this pass, on the
current working tree, with `platform/.venv` (the venv root's `.venv` does *not* carry the platform
dependencies — `fastapi`/`numpy` are missing there, and a run against it errors at collection).
Rows marked *carried* are earlier passes' evidence, not re-executed here.

**What the seventh pass could and could not re-run.** The tree is unchanged since `085b68f`
(2026-10-04), which makes the re-runs directly comparable: **every number below reproduced
exactly**, which is the strongest evidence this file has had that the sixth pass's numbers were
real. Two things were deliberately **not** re-run, and both need something this box does not have
right now:

* **The carrier acceptance** (`scripts/carrier_acceptance.py`) needs a live public tunnel *and* a
  running instance; neither is up (the box is running the *sibling* product on 8000/8501 — the
  isolation scenario the guard exists for — and no ANITA instance is listening). Its 15/15 result
  stands as sixth-pass evidence and is unchanged in code.
* **The browser suites** (`tests/e2e/run.sh`, `run_voice.sh`) need a fresh instance of their own;
  the sixth-pass 50/50 and 4/4 stand.

The live-Ollama prose check was also not re-run (a CPU/GPU-bound generation), and the one new
signal the pytest run produced is a **warning, by design**: `voice_engine_manifest.json` reports
that the *sibling* repository has moved on since the manifest was generated. That is the CL-11
behaviour working — the copy is still checked against the pinned manifest, and a sibling's own
evolution is not this product's regression.

| Check | Command | Result |
|---|---|---|
| Unit suite — **re-run seventh pass, after all seven fixes** | `platform/.venv/Scripts/python -m pytest tests -q` | **1194 passed**, 1 skipped, 0 failed, in 42 s — 1163 on the untouched tree (byte-identical to the sixth pass's figure), +13 for §8.30/§8.31 and +18 for §8.32–§8.36. Two warnings: a Starlette/httpx deprecation, and the CL-11 sibling-drift warning working as designed |
| §8.30–§8.35 reproductions — **added seventh pass** | revert each fix, re-run | **3, 2, 1, 1** for §8.30/§8.31, then **6, 4, 1, 1** for §8.32–§8.35 — the relay never arming the window, the frame-path guard absent, `end_turn` clearing the flag, the flag never consumed, the status callback releasing on progress, the citation verdict dropped, the retry reading only memory, and the report written on a non-isolation refusal. Every reproduction is real, not asserted |
| Config audit — **re-run seventh pass** | `anita.boot.preflight()` → AUDIT phase | **43 keys live · 5 pending**, reproduced exactly. The five are `DATABASE_URL` + `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD` — CR-02's keys, which really have no code (`files_scanned=50`, `inert=[]`) |
| Carrier-free acceptance (US-001) — **not re-run** | `platform/.venv/Scripts/python scripts/carrier_acceptance.py` | Needs a live public tunnel **and** a running instance; neither is up. Sixth-pass result: **15/15** over the public tunnel (14/14 cold, warm re-run 15/15). Drives Twilio's half of `T-01/T-05/T-06/T-12/T-13/T-16` and prints the handset checklist. `--check` verifies preconditions only |
| System integrity — **re-run seventh pass, after the fixes** | `platform/.venv/Scripts/python scripts/system_check.py` | **19 passed · 0 failed · 0 warnings · 1 deferred** — 49 modules import, 47/49 reachable, 11 dependencies wired; UC-02/UC-03 *driven*; UC-14 `NOT CONFIGURED`; UC-09 deferred. It was **20 passed** earlier in the same pass; the difference is **UC-04, now `NOT TRIGGERABLE HERE — voice shed at level voice_shed`**, and that is the capacity ladder working rather than a failure. `nvidia-smi` reads **446 MiB free of 16 311** against `sibling_floor_mib: 6000` because the sibling product's Ollama models are resident; sampling `CapacityPolicy` directly logs `capacity degrade → voice_shed`. No failures either way |
| Evaluation set — **re-run seventh pass** | `platform/.venv/Scripts/python scripts/release_gate.py --no-browser` | **70 / 70 cases passed** — from the **frozen** set. The SHA-256 was recomputed independently this pass: `eb2ffc27c88cf18ed872d042b2f93b6fdae20efe3f7911dfd820a2e9173164dd`, matching the pin in `config/anita.yaml:183` exactly, 70 cases |
| Citation integrity (US-012) — **re-run seventh pass** | `scripts/release_gate.py` section 1 | **55 grounded answers, 0 uncited, 0 incomplete** — reproduced, and now written into `data/out/reports/release-gate.json` as `citations: {grounded: 55, uncited: 0, incomplete: 0}` (§8.24 verified) |
| Boot preflight — **re-run seventh pass** | `anita.boot.preflight()` | **OK**, exit 0. LOAD · AUDIT · ISOLATION (`db=anita_ams/anita collection=anita_ams__v1 ports=8100,8601,8602,8085`) · SECRETS · PLATFORM (`device=cuda backend=ollama model=qwen2.5:14b voice_capable=True — NVIDIA GeForce RTX 5060 Ti`) · DATA (**141 chunks**) · INDEX (**live collection `anita_ams__v5`**) · WARM (ollama reachable) all green. **One** degradation: SMTP unconfigured. The sixth pass's "36 keys live" in this row was wrong — the audit says 43, and the same table already said so one row above |
| Plan-artifact validator — **added seventh pass** | `node …/validate-artifacts.mjs docs/sdlc` | **1197 passed / 10 failed → 0 failed after this pass's fix.** All ten were one class: this file's §3 used `### MOD-0x <name>` headings, which the validator reads as an ID *definition*, colliding with `05-modularization.md`'s. Headings renumbered to `### 3.x MOD-0x — <name>`; the file no longer defines IDs it only reports on |
| Voice wiring harness — **not re-run** | `platform/.venv/Scripts/python scripts/voice_e2e.py` | Sixth-pass: all control stages **PASS** (modules, deterministic price, VAD end-of-speech, capacity refusal, voice-sheds-first); model stages need `--models` |
| Browser suite (buyer chat, Streamlit) | `bash tests/e2e/run.sh` | *carried*: **50 passed** (25 scenarios × desktop + mobile) — on a frozen tree, fresh instance |
| Browser suite (browser voice, FastAPI page) | `bash tests/e2e/run_voice.sh` | *carried*: **4 passed** (2 voice scenarios × desktop + mobile) |
| Transcript persistence | US-006 | *carried*: **48-turn transcript, contiguous seq 1…48, read through a restarted instance** |
| Cross-channel identity | US-004 | *carried*: chat price question → lead captured with the buyer's number → a voice turn from that number answered *"The minimum order quantity for Ultrasound Gel is 10 Pieces"* |
| Index promotion (US-010) — **re-run after the §8.23 fix** | `platform/.venv/Scripts/python scripts/rebuild_index.py` | Promoted **`anita_ams__v5`, 141 vectors embedded and validated**; the build row records `all 70 cases passed` against set `eb2ffc27…`. With **no serving process running**, v5 already held its 141 vectors immediately after promotion — under the old code it would have been empty until a process lazily filled it, unvalidated. A fresh assembly then logged `index pointer: serving promoted collection anita_ams__v5` with `store.count() == 141`. *(Fifth-pass evidence, now superseded: two rebuilds of 4.3 s each and 20/20 queries served during a promotion — the "70/70 before promotion" was measured against the previous collection, which was the defect.)* |
| Voice answer path | see §3a | *carried*: grounded answer, price **with** caveat, greeting, help, decline |
| Simulated call | see §3a | *carried*: full media-stream round trip over the public tunnel |
| Live prose leg | US-014 | *carried*: real Ollama + real corpus — accepted, grounded, 7.8 s |
| Barge-in | US-017 | *carried*: remaining clauses stopped, `clear` sent once, transcript marked |
| WhatsApp ingress | US-002 | *carried*: signed message → answered → `outbound_send` row; 10 redeliveries → 1 reply (unit); driven in `system_check` |
| Browser voice session | US-003 | *carried*: live — `browser_mic` session row opened, closed `client_stop`, slot released |
| Release gate | `scripts/release_gate.py` | with an instance: runs; without one: **`[NOT RUN] … not a product failure`**; with `--no-browser`: **promotion refused** (an unrun suite is not evidence) — see below |

**A Playwright defect worth recording (2026-10-04, US-008).** The voice e2e first failed with
`NotSupportedError: Not supported` from `getUserMedia` under a granted permission and a fake
device. The cause is not the product: Playwright's default headless **shell**
(`chromium-headless-shell`) ships without a media stack. The full browser (`channel: "chromium"`)
works with the same flags. The voice spec pins that channel, scoped to the file so the Streamlit
scenario suite keeps its default.

**Two more runs that were not evidence (2026-10-04, US-006).** One browser run failed two
scenarios ("a greeting is greeted", "a price question is not answered with a FAQ") and the
instance log ended in `Fatal Python error: _PySemaphore_Wakeup: parking_lot: ReleaseSemaphore
failed` — alarming in a log, and neither was the product's fault: the release gate was running
its 70-case evaluation on the same CPU at the same time, and the fatal error is a CPython 3.14
runtime fault in **Streamlit's own `watchdog` thread** (the trunk of that trace is
`watchdog/observers/winapi.py`; every one of our threads is parked in `threading.wait`). On a
quiet machine the same suite is **50/50**, and the two assertions hold in-process
(`Hello — how can I help?`, `₹60/Piece … quotation`). The lesson is the one §7 already learned
once: before believing a red browser run, check what else is on the machine.

**A verification defect found and fixed on 2026-10-04 — the harness, not the product.** Two
browser runs failed with wrong answers ("a greeting is greeted" answered from the catalogue; a
spec question answered with an empty bubble) and looked exactly like product regressions. They
were three defects in `tests/e2e/run.sh` stacked on each other:

1. **It killed only the parent PID.** Streamlit is parent + child; the child kept port 8085, so
   the *next* run silently drove the previous instance — whose log line `Port 8085 is not
   available` went unnoticed. Cleanup now kills the tree, and a **port preflight refuses to test
   an instance the script did not start** (exit 2).
2. **The instance inherited the pipeline's stdout**, so anything piping `run.sh` (CI logs, this
   session) waited for the instance to die — which is why finished runs looked "still running".
   The redirection now applies to the subshell.
3. **Editing `app/` files while a run is live** makes Streamlit's watcher reload modules, which
   duplicates the `Intent` enum inside the cached assembly and breaks `intent is Intent.X` — one
   edited run failed, and the next inherited the same damaged server through (1).

With the tree frozen and a fresh instance the suite is **50/50 green**. The lesson is recorded
because it cost most of an afternoon: *a browser failure is only evidence about the product when
the instance under it is the one you just built.* (A footnote from 2026-10-04: `taskkill /T` still
left a survivor twice that day — Streamlit re-execs itself, and the new process is no longer in
the killed tree. The guard turns that into a refusal rather than a false green, which is the
property that matters; the leak itself is still there.)

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

### The gate's "degraded" lines are themselves an artifact (§8.5)

The same run printed:

```
degraded: TWILIO_AUTH_TOKEN unset — inbound webhooks cannot be signature-verified
degraded: TUNNEL_HOST unset — Twilio has no address to call back…
```

**Both are false.** The preflight on the same machine reports zero degradations and both values are
set in `.env`. The gate builds its assembly with `assemble(require_preflight=False)`, and
`assembly.py:196` is `settings = load_settings(env or {})` — passing `{}` explicitly *suppresses*
`.env` loading. Every caller that skips preflight (the gate, `system_check`, the Streamlit app, the
dev scripts) therefore runs against an **empty environment**. Tests rely on that isolation, so the
fix belongs in the callers, not the library: release tooling should build from the real environment
(`env=load_settings().env`) instead of relying on the skip-preflight default.

---

## 8. Defect register — found by the 2026-10-04 call-site audit

Ordered by severity, not by module. Everything here was reproduced or verified in code; the
`§`-references in §3 point back to these.

### Code defects

| # | Defect | Where | Impact |
|---|---|---|---|
| 8.1 | ✅ **Resolved 2026-10-04 by US-022.** Only one handoff could ever be created per database: `handoff_id=""` was inserted verbatim, so the first row took the empty PRIMARY KEY and every later one failed the UNIQUE constraint, swallowed into `dispatched=False`. The handoff's `lead_id` was also the conversation id | `escalation.py`, `crm.py` | The store assigns the id, `conversation_id` is its own column (guarded migration), and the real `lead_id` is passed — empty when anonymous, never a conversation pretending to be a lead |
| 8.2 | ✅ **Resolved 2026-10-04 by US-022.** Rep notification did not exist: no SMTP code in `app/`, `state='notified'`/`notified_at` never set, and no admin route exposed handoffs | `crm.py`, `notify.py`, `surfaces.py` | `anita/notify.py` emails the rep over SMTP; a delivered notification moves the handoff to `notified` (best-effort — a broken notifier leaves it `open` and never fails the turn); `GET /api/admin/handoffs` and a console panel show the queue |
| 8.3 | ✅ **Resolved 2026-10-04 by US-017; reachable since §8.30 (2026-10-10).** Barge-in did not stop clause synthesis: interrupting during clause 1 still synthesised and sent every clause, because the cancel flag was read only before the first clause and after the last | `voice.py`, `voice_rag.py` | The clause loop checks the signal **per clause** (raising `GenerationCancelled`, so no tail flush), `_synthesise_streamed` and the TTS producer stop mid-clause, and a partial is never cached. **Qualification:** correct and unit-tested since Batch 7, but the cancel signal had no live producer on a call until §8.30 wired `mark_speaking` — the fix was complete; the trigger was not |
| 8.4 | ✅ **Resolved 2026-10-04 by US-017; reachable since §8.30 (2026-10-10).** `clear_playout()` had no caller, so Twilio's buffered audio was never cleared on a barge-in | `media_stream.py` | The relay sends `clear` on the transition into an interruption — once per barge-in (echo/muted frames and the caller's continuing speech do not re-signal) — and marks the transcript truncated. **Qualification:** the call site was correct and unit-tested from Batch 7, but nothing produced the transition from audio until `send_frames` armed the engine's SPEAKING window (§8.30) |
| 8.5 | ✅ **Resolved 2026-10-04.** `assemble(require_preflight=False)` dropped `.env` — `load_settings(env or {})` converted `None` to `{}`, which suppresses `.env` loading — so the gate, `system_check`, Streamlit and the dev scripts scored a default assembly and the gate printed two spurious "degraded" lines | `assembly.py` | Fixed by adding `assembly.deployed_environment()` (`.env` overlaid by the real environment, exactly what the boot uses) and having the seven tooling callers pass `env=deployed_environment()`. The test default stays an **empty** environment on purpose — a developer's `.env` must never change a test's result — and `test_the_tooling_passes_the_deployed_environment` guards the wiring |
| 8.6 | ✅ **Resolved 2026-10-04.** The media-stream WS performed no admission of its own: it accepted any connection and the relay only ever *released* a slot, so a direct `wss://` connection drove STT/TTS uncapped and a forged `call_id` released a live call's slot. **Fix:** the webhook mints an HMAC token for the admitted call (`voice_webhook._stream_token`) and stamps it into the TwiML as a `<Parameter>`; the relay verifies it *and* checks `AdmissionController.holds(call_id)` before feeding a single frame, closes a refused socket with **1008**, and releases nothing | `voice_webhook.py`, `media_stream.py` (`_refusal`), `speech.py` (`holds`), `server.py`, `assembly.py` | **Reproductions** (each fails without the gate): a tokenless stream is refused and the engine sees 0 frames; a token minted for another call is refused; a stream for a call holding no slot is refused; and a refused socket naming a live call leaves that call's slot intact. Verified over the real ASGI socket as well as the relay object |
| 8.7 | ✅ **Resolved 2026-10-04 (Batch 0, US-025); re-verified sixth pass.** All three clauses are fixed: `new_trace(call_id, turn_no)` is positional (`voice_trace.py:40`, called `voice.py:1166-1170`), `TurnTrace` has `mark()`/`note()`/`stages_seen`/`durations_ms` (`observability.py:94,107,121,126`), and `.emit(` has production call sites — `surfaces.py:314` (chat) and `voice.py:1132-1134` → `voice_trace.py:58` (voice, reached from `media_stream.py:319-321` and `browser_voice.py:394`). **Seventh pass:** the sixth pass's `:315` and `:1113-1115` were off by one and by a function (`:1113-1115` is inside `call_expired`). This entry contradicted the US-025 `COMPLETED` row in §3; the row was right | `voice_trace.py`, `observability.py`, `voice.py`, `surfaces.py` | Traces are written on every turn outcome in production |
| 8.8 | ✅ **Resolved 2026-10-04 (Batch 0, US-027).** `eval/anita_frozen_set.jsonl` exists (70 cases); its SHA-256 (`eb2ffc27…`, re-computed this pass) matches the pin in `config/anita.yaml`; `load_eval_set()` has real callers — `assembly.py:220-240`, which **fails closed** on missing/empty/hash-mismatch, and `scripts/freeze_eval_set.py`. `cases_from_corpus()` no longer runs at boot; it survives only in the regeneration tool. **Residue:** `scripts/release_gate.py:81` still imports `cases_from_corpus` unused | `observability.py:535`, `assembly.py:220-240`, `eval/anita_frozen_set.jsonl` | A corpus change can no longer silently change the test set |
| 8.9 | ✅ **Resolved 2026-10-04 (Batch 0, US-027).** `passed` is derived: `passed=False` is set for a critical failure, a sub-floor grounded rate, or a regression over tolerance, and `passed=True` is reached only after all three rules pass; a tolerated non-critical failure is named in `reason` instead of being reported as "all N cases passed" | `observability.py:454-525` | The latent false-green is gone. (The register previously carried no ✅ here although the code was fixed — corrected this pass) |
| 8.10 | ✅ **Resolved 2026-10-04 (Batch 7).** The call passed the asset path and a `call_sid` to `note_asset_played(self)`, which takes neither — a `TypeError` that would have fired the first time a deployment configured a refusal asset. The outcome axis is aggregate, so the call is now `REGISTRY.note_asset_played()` | `voice.py` (`_speak_fixed_response`) | The latent TypeError is gone; the degraded-vs-failed distinction the method exists for now works on the branch that uses it |
| 8.11 | ✅ **Resolved 2026-10-04 by CL-1…CL-5 (§10); re-verified sixth pass.** `CLOSING_REPLY` is ANITA's (`voice.py:599`), `_CORRECTIONS` is catalogue vocabulary (`voice.py:1451-1464`), `_CLARIFICATION_RE` matches product/model/item (`voice.py:356-362`), `_META_MARKERS` says "catalogue profile" (`voice.py:541-553`), the prompt builder is a catalogue assistant (`voice.py:1723-1787`), and the logger is `anita.voice` (`voice.py:46`). Remaining "Meridian"/"university" hits are historical comments only | `voice.py` | Note the isolation guards that *list* sibling values (`config.py:127`, `index.py:76`, `config/anita.yaml`) are deliberate rejection lists and must stay |
| 8.12 | ✅ **Resolved 2026-10-04 (Batch 7).** `IndexLifecycle` gained a production caller (`scripts/rebuild_index.py`), and the dead symbols went: `FRAME_BYTES` and `OFFLINE_TEXT` were removed outright, `AdminSurface.traces` (a writer stored, never read) was removed rather than left implying an endpoint, and `speech.EndOfSpeechTracker` **stays** — it is the diagnostic harness's VAD (`scripts/voice_e2e.py`), not a second product implementation, and the module says so | `index.py`, `media_stream.py`, `answer_engine.py`, `speech.py`, `surfaces.py` | The "looks wired, isn't" surface area is gone. One intentional keeper, documented |

| 8.23 | ✅ **Resolved 2026-10-04 (same day it was found).** The index rebuild did not embed into the staging collection it names, so the gate validated the *previous* index and the promoted collection was filled in later, unvalidated. **Fix:** `index.staging_retriever()` retargets the embed to `build.collection`, `SharedRagRetriever.for_collection()` supplies a sibling bound to that collection, and `rebuild_index._gate_validator` points `chat.retriever` at the same sibling for the run and restores it afterwards, including on failure | `index.py` (`staging_retriever`, `build_index`), `rag_adapter.py` (`for_collection`), `rebuild_index.py` (`_gate_validator`) | **Evidence:** a real rebuild promoted `anita_ams__v5` with 141 vectors embedded and validated; with no serving process running, v5 held its 141 vectors immediately after promotion; a fresh process logs `serving promoted collection anita_ams__v5` with `store.count() == 141`. **Reproduction:** removing the retarget makes the second rebuild embed into `v1` instead of `v2` and the new test fails |
| 8.24 | ✅ **Resolved 2026-10-04 (Batch 7).** The citation counts were computed, printed, and dropped before the verdict. `combine_validation` now takes them: a non-zero `uncited`/`incomplete` count — or a check that did not complete — refuses promotion and names the reason, and the counts travel into the JSON report (verified: `{'grounded': 55, 'uncited': 0, 'incomplete': 0}` is now in `release-gate.json`, which used to omit them entirely) | `observability.py` (`combine_validation`, `_citation_failure`), `release_gate.py` | A provenance regression can now refuse a release, which is what the `ChatReply` comment always claimed |
| 8.25 | ✅ **Resolved 2026-10-04 (Batch 7).** The voice leg called `begin_turn` directly, so US-006's per-conversation slot covered chat and messaging but not a conversation a buyer had also touched by phone. The pipeline now resolves the conversation and runs the turn through `submit_turn`, exactly as `ChatSurface` does; `QueueFull` (20 turns waiting on one conversation) is logged and answered inline rather than leaving a caller in silence, because a phone line cannot be re-prompted the way a 429 re-prompts a browser | `voice_pipeline.py` (`_answer`) | Reproduced as a test: six concurrent voice turns now come out `[user, assistant] × 6` and the spy test asserts `submit_turn` is the path |
| 8.26 | ✅ **Resolved 2026-10-04 (Batch 7).** The 15-minute rung was scheduled by nothing. The constants now distinguish the waits from the ceiling: five **attempts** (the send + four retries) with waits 5 s / 30 s / 2 m / 8 m, and `BACKOFF_CEILING_SECONDS = 900` as the documented 5 s → 15 min cap — defined for any caller that asks, never scheduled | `outbox.py` | The code, the tests and MOD-01's "5 attempts, 5 s → 15 min" now say the same thing |
| 8.27 | ✅ **Resolved 2026-10-04 (Batch 7).** `AdapterRegistry` was built with no probe, so "declares → validates → **probes** → enables" skipped its third step in every deployment. `credential_probe(env)` now supplies one: the provider-neutral precondition every adapter shares is its HMAC secret, and a missing credential raises `AdapterUnreachable` naming the key (never the value) before the adapter is registered | `extra_channel.py` (`credential_probe`), `assembly.py` | A channel declared without its credential now refuses to enable at boot instead of authenticating nothing at the first inbound |
| 8.28 | ✅ **Resolved 2026-10-04 (Batch 7) — every symbol called or removed.** *Wired:* `expire_transcript` + a new `transcripts_due_for_expiry` query became the janitor's retention duty (`DG-11`, `retention_days`, 0 = disabled), so the review's `expired` state is reachable in production; `unsummarised_closed` became the janitor's summary retry, so a summary lost to a transient store failure is retried instead of lost. *Removed:* `conversations_for_identity` (no caller), `resolve_whatsapp_session` (no caller, and buggy), the CRM outbox seam and its `crm_outbox` table (a second queue nothing produced or consumed; the live one is `outbox.SendOutbox`), `AdminSurface.traces` (a writer stored and never read), `FRAME_BYTES`, `OFFLINE_TEXT` | `conversation_store.py`, `orchestrator.py` (janitor), `crm.py`, `surfaces.py`, `media_stream.py`, `answer_engine.py` | New tests cover retention expiring a transcript while keeping the row, retention off when no window is configured, and a lost summary being retried on the next tick |
| 8.29 | ✅ **Resolved 2026-10-04.** Three fixes, one of which found a real defect: (1) `mapping_get` now matches the receiver names the platform actually uses for the environment (`env`, `environment`, `environ`, `values`, `secrets`) — deliberately a short allowlist, because a catch-all would score `payload.get("STATUS")` as a read and a false LIVE is the one error this audit cannot afford (a test pins both directions); (2) `ANITA_EXTRA_CHANNEL_SECRET` joins `STRUCTURAL_READ_KEYS` — its name is a *value* from `config/anita.yaml`, so no regex can follow the read; (3) **`OLLAMA_KEEP_ALIVE` was genuinely unread** — every sibling model key took an environment override and it did not, so `.env.example` declared a knob that did nothing. It now reads from the environment like the rest. **Result:** the boot audit reports **43 live · 5 pending**, and the five are the `DB_*`/`DATABASE_URL` keys of CR-02, which really do have no code yet | `config_truth.py`, `config.py`, `notify.py`, `extra_channel.py` | The boot report no longer calls modules unbuilt; the `PENDING` bucket is now trustworthy as "CR-02's keys" |

### Code defects — found by the seventh pass (2026-10-10)

The tree is unchanged since `085b68f`, so these are not regressions: they are what a second audit of
the same code finds when it asks *who calls this* rather than *does this exist*. Ordered by severity.

| # | Defect | Where | Impact |
|---|---|---|---|
| 8.30 | ✅ **Resolved 2026-10-10, the day it was found.** **The barge-in detector had no live trigger.** `mark_speaking()` — the only writer of `STATE_SPEAKING` and the only caller of `note_playback()` — had **zero call sites** repository-wide, so `admit_inbound_frame` returned `FRAME_LISTENING` unconditionally and every consumer of its verdict was dead on a real call. **Fix:** `send_frames` (both relays — `media_stream.py` and `browser_voice.py`, the one choke point each transport's outbound audio passes through) calls `mark_speaking()` on the turn's **first** batch and `note_playback()` on every batch after it; `_run_turn`'s `finally` calls `end_turn()`. On the engine side `end_turn()` no longer clears `_turn_began_as_interruption`, and `process_utterance` consumes that flag exactly once, where it decides the min-burst floor. **A latent leak was closed with it:** the flag had no live clearer at all, so a single interruption would have held every later utterance on the call to the interruption floor. **Evidence:** 13 new tests (5 relay window, 4 engine lifecycle, 3 frame-path gate, 1 browser relay); the end-to-end one drives a **real `VoiceCallSession`** through the relay with neither the verdict nor `_speaking` stubbed. Every reproduction probed by reverting the fix — 3, 2, 1 and 1 tests fail respectively | `voice.py:776,790,793,855,945,1243-1258` · `media_stream.py:329` · `browser_voice.py:402` | **US-017 restored to `COMPLETED`.** On a telephone call the assistant now stops speaking when the caller talks over it. The gate's *thresholds* are confirmed on the real carrier call (§3a) |
| 8.31 | ✅ **Resolved 2026-10-10.** **The media-stream gate guarded the `start` event, not the frame path.** `self.refused` is initialized `""` and set only inside `_on_start`; a client that opened the public socket and sent `media` frames without ever sending `start` was never refused and reached `feed_audio` with `call_id == ""`. **Fix:** `_on_media` refuses `media before start` — the receive loop then closes with 1008 and `close()` releases nothing, because `refused` is set. The rule is protocol as much as security: without `start` the engine has no call identity, so the conversation would be keyed on an empty string. Twilio always sends `start` first, so nothing legitimate is refused, and the refusal holds even with no verifier wired. **Evidence:** 2 new tests (refused with a verifier installed, and refused with no gate at all); both fail without the guard | `media_stream.py:204-222` | Corrects the §8.6 ✅: the gate is now whole. "No unauthenticated stream drives the engine" is finally true |
| 8.32 | ✅ **Resolved 2026-10-10.** **`status()` released the admission slot on *any* status callback, not only terminal ones.** It released whenever `CallSid` was present, with no check of `CallStatus` — while its own docstring said "released on any terminal status". Harmless under Twilio's default callback set, but a number configured with `statusCallbackEvent` including `ringing`/`answered` would release a **live** call's slot mid-call and the 2-call ceiling would quietly stop holding. **Fix:** `TERMINAL_CALL_STATUSES` (completed / busy / no-answer / failed / canceled) is the only set that releases; known progress statuses are kept and logged at debug, and an unrecognised or absent `CallStatus` keeps the slot and warns — keeping it is the recoverable error, because `release_stale` sweeps it, while freeing a live call's slot is not. **Evidence:** 7 new tests — all five progress statuses parametrised, the absent-status case, and every terminal status releasing. Reverting the guard fails 6 | `voice_webhook.py:32-46,235-278` | The ceiling now holds under any callback configuration, not just the default one |
| 8.33 | ✅ **Resolved 2026-10-10.** **Citation integrity could not refuse an index promotion.** §8.24 wired the counts into `combine_validation`, but only the *release-gate script* passed them: `rebuild_index.py` validated through `QualityGate.evaluate` alone, which asserts answer text and never sees provenance — so a rebuild could promote a collection while uncited answers existed, the one moment the check was written for. **Fix:** the counting loop was extracted to `observability.citation_integrity` (one definition), `_citation_failure` became public `citation_failure`, the release gate now calls the shared counter, and `rebuild_index._gate_validator` folds the counts into the verdict it returns — so `build_index` refuses the promotion through the path it already had. **Evidence:** 4 new tests incl. a rebuild refused with one uncited answer and one with an incomplete citation; reverting the fold-in fails 4. `release_gate.py --no-browser` re-run: 70/70, 55 grounded, 0 uncited, 0 incomplete — unchanged | `observability.py` (`citation_integrity`, `citation_failure`) · `rebuild_index.py:49-88` · `release_gate.py:106-117` | §8.24's claim is now true of a promotion as well as of a gate run |
| 8.34 | ✅ **Resolved 2026-10-10.** **The janitor's summary retry could not repair the case it exists for.** It summarised only conversations still in `registry._by_key`, so in a process that had just started — when that map is empty — every id `unsummarised_closed` returned was filtered to nothing and the row stayed unsummarised for good. It also missed the rows `sweep_idle` closes for conversations this process never held — the exact case. **Fix:** `ConversationRegistry.from_store(id)` rebuilds one conversation from its own rows whatever its state (the *record* path, where `recover` is the lookup path), and `_load_all_turns` fills every turn with its classification and channel — `_seed_context` keeps only the tail because it feeds a prompt, and a summary is a record that must not under-report. `retry_summaries` falls back to it, and a new `registry.by_id` replaces the two hand-rolled `_by_key` scans. **Evidence:** 4 new tests — a restart summarising what the old process could not, a rebuilt summary counting all 12 turns rather than the 6-turn context tail, an unknown id being a miss not an error, and an unreadable classification not losing the turn. Reverting the fallback fails 1 | `orchestrator.py` (`by_id`, `from_store`, `_load_all_turns`, `retry_summaries`, `sweep_idle`) | A summary lost to a restart is now recoverable, which is what `summary_for`'s docstring always claimed |
| 8.35 | ✅ **Resolved 2026-10-10.** **US-030's "a refused boot leaves no artifact behind" was narrower than the story.** `boot.write_report` suppressed the report only when the **ISOLATION** phase failed, so a refusal from AUDIT, SECRETS, DATA or INDEX still wrote `logs/anita/preflight.json` into the directory it had just refused to boot in. **Fix:** the suppression is `if not report.ok` — every refusal, not just the one the acceptance test demonstrates. **Evidence:** 2 new tests — a non-isolation refusal writing nothing, and a clean boot still writing its report (so the suppression is about refusals, not shyness); reverting fails 1 | `boot.py:370-393` | The claim now holds generally |
| 8.36 | ✅ **Resolved 2026-10-10.** **Two runtime artefacts were tracked in git.** `platform/logs/anita_crm.sqlite` (the live CRM database) and `platform/logs/anita/preflight.json` — the latter still asserting `"25 key(s) live, 16 pending (modules not yet built)"` from the first commit, contradicting the current `43 live · 5 pending`. **Fix:** both `git rm --cached` (the files stay on disk), now ignored by the `logs/` rule `platform/.gitignore` already carried; and `platform/README.md`'s stale "1156 tests" corrected to the current figure. **Evidence:** `git check-ignore` confirms both paths are ignored and `git status` is clean of them — the tree no longer dirties on every run | `platform/logs/` · `platform/README.md:31` | The permanent `M` in every `git status` is gone, and no committed report contradicts the code |

### Documentation defects

**Sixth pass: all five re-verified still open. Seventh pass: all five are now resolved** — §8.13,
§8.14, §8.16 and §8.17 by the stale-document commit (`a81f54e`), and §8.15, the last one, was the
table being the wrong half: the runbook had already been rewritten to "What still needs a real call"
and §9 already recorded it as done. Each one removed a place where a document contradicted the code
it describes.

| # | Defect | Where |
|---|---|---|
| 8.13 | ✅ **Resolved 2026-10-04.** The migration table now marks MOD-01…MOD-10 ✅ **done** with the module paths as evidence, and CR-01 reads **DONE — 10/10 stories, C1–C9 PASS**. The duplicate MOD-01/02 row is gone; the file points at `work-status.md` for per-story detail rather than restating it | `plan-state.md` §Migration status, §Change requests |
| 8.14 | ✅ **Resolved 2026-10-04.** "Next action" now says **141 chunks** (CV-04's correction) and, more usefully, is no longer a migration plan at all: all ten modules are built, and the section says what is actually next — US-001's carrier call, plus the items blocked on people | `plan-state.md` §Next action |
| 8.15 | ✅ **Resolved — confirmed seventh pass.** "What is still missing" said TAC-6 and AC-7 were not implemented; both were implemented in `4fe32b0`. The section is now **"What still needs a real call"** (`platform/docs/twilio-voice-setup.md:187-192`) and says so explicitly. The sixth pass listed this as open while §9 recorded it as done in Batch 7 — the table was the stale half | `platform/docs/twilio-voice-setup.md` |
| 8.16 | ✅ **Resolved 2026-10-04.** The matrix now reads: Vector `COMPLETED` (Chroma persisted, measured), Corpus/CRM/Sentiment `IN PROGRESS` — CR-01 done, CR-02 not started — with a correction note recording that only the CR-02 column was ever genuinely unstarted | `change-requests/STATUS.md` |
| 8.17 | ✅ **Resolved 2026-10-04; the count in it went stale immediately.** The README table marks MOD-01…MOD-10 ✅ **built** (the duplicated "next"/"planned" rows are gone) and names a test count with the command and a pointer to `work-status.md` §9. **Seventh pass:** the count it wrote is **1156** and the suite now reports **1163 passed / 1164 collected** — carried as §8.36 rather than silently corrected here, because the README is not this file's to assert | `platform/README.md` |
| 8.18 | ✅ **Resolved 2026-10-04 by CL-6/CL-9 (§10).** `shared/sentiment/core.py` named its logger `"anita.sentiment"` — a product-name leak in a package that must be product-agnostic; now `shared.sentiment`, and the fallback prompt is the generic constant | `shared/sentiment/core.py` |
| 8.19 | ✅ **Resolved 2026-10-04.** The gate could not tell "no instance" from "product broken": with nothing listening it ran Playwright blind, the serial spec skipped the other 49, and it reported "1 scenario failed". Now it probes the instance's health endpoint first and returns `[NOT RUN] 0 scenarios — no instance on <url> … this is an environment problem, not a product failure`; the verdict still refuses promotion (an unrun suite is not evidence) but `combine_validation` says "browser suite did not run" instead of `failed: []`. **Not covered:** a *stale* instance is still an instance — verified live, a gate run found one failing scenario against a Streamlit process started at 10:41, predating the day's commits | `scripts/release_gate.py` |
| 8.20 | ✅ **Resolved 2026-10-04 by US-002/US-003/US-004.** The UC-02/03/04/14 deferral reasons had become factually wrong — "no webhook route is served", "no audio path", "an extension point, verified by design review" were all false once the routes were built. The four entries are removed (only UC-09 remains deferred, which is still true) and each use case is now *driven* by `system_check` instead of asserted | `config/anita.yaml` → `release.deferred_use_cases`; `scripts/system_check.py` |
| 8.21 | ✅ **Resolved 2026-10-04 by US-004.** **US-002's AC-2 was never implemented**: a commercial follow-up that names no product ("and the price?", "what about the MOQ?") declined, because product resolution read only the current message — so the second question of a conversation was refused. Found while wiring cross-channel context, which is exactly what makes it visible. `_follow_up_product` now resolves from the conversation's turns (including seeded cross-channel context) **only** when the message names nothing beyond fact words and filler (`_names_no_product`); a message naming an unrecognised product still declines, because answering about whatever was discussed earlier would quote a real price for the wrong product. Verified live: web-chat price question → voice "what is the minimum order quantity?" → *"The minimum order quantity for Ultrasound Gel is 10 Pieces."* | `answer_engine.py` `_commercial` |
| 8.22 | ✅ **Found and resolved 2026-10-04 by US-012's citation check.** Two real defects, neither visible from the text of an answer: the **listing branch** ("what do you sell") stated a catalogue fact with no citation, and the incomplete-citation rule was flagging *record* citations ("company:branches") for having no URL — when a record id is precisely how a statutory fact resolves (`TAC-6`). Both were found by running the frozen set a second time counting provenance; neither would have been caught by the gate, which sees text. | `answer_engine.py` (`listing`, `ChatReply.cite`), `scripts/release_gate.py`. Now: 55 grounded answers, 0 uncited, 0 incomplete — printed on every gate run, including at zero, because a check that only speaks when it fails cannot be told from one that never ran. (**Sixth pass:** the count is printed but not yet part of the verdict — §8.24) |

---

## 9. Pending work — the completion sequence

The full plan — per-story work, exit criteria, sizes, risks — is `completion-plan-us-001-030.md`
(user-approved 2026-10-04). This is the same sequence in one screen.

### Everything still open, in one list

Updated 2026-10-10 (seventh pass, same day as the fixes). **The sixth pass's closing line — "not one of
them is something an agent can finish alone" — is no longer true**, and the seventh pass proved it:
all seven of its defects were unblocked code, and all seven are closed with reproductions.

| # | Item | Waiting on | Who |
|---|---|---|---|
| 1 | ~~**§8.30 — wire the barge-in trigger**~~ | ✅ **Done 2026-10-10.** Both relays arm and close the engine's SPEAKING window; the flag lifecycle is consumed where it is read. US-017 restored to `COMPLETED` | — |
| 2 | ~~**§8.31 — move the media-stream gate onto the frame path**~~ | ✅ **Done 2026-10-10.** `media before start` is refused; the §8.6 invariant is now whole | — |
| 3 | ~~**§8.32–§8.36**~~ | ✅ **All five done 2026-10-10.** Terminal-only slot release; the citation rule shared with the promotion path; the summary retry rebuilt from the store; no report on any refusal; both runtime artefacts untracked and the README count corrected | — |
| 4 | **US-001's real carrier call** — the handset half of `T-01…T-17`, a real CallSid in `inbound_event`, the timing budgets on a real network, and **the barge-in thresholds on a real handset and echo path** (§8.30's residual) | a phone, the Twilio console, and a decision to open the public tunnel | **You** |
| 5 | **US-021 outbound worker** | DG-08 legal review | Blocked |
| 6 | **CR-02 Postgres + Docker** | decisions Q9–Q12 (§5) | Blocked |
| 7 | **DG-08 legal review** of the clinical-refusal wording | a lawyer. Blocks public launch | **You** |
| 8 | **U-1 / U-2 / U-3** — three product questions (§6) | your answers | **You** |
| — | Housekeeping: set `SMTP_*` in the deployment (a value, not code) | deployment values | you |

**Nothing in this table is an engineering task any more.** Every defect the audit produced is closed;
what remains is one acceptance run that needs a handset, two builds blocked on decisions, and four
answers.

#### Carrier-free acceptance — 15/15, run 2026-10-04

`scripts/carrier_acceptance.py` drives Twilio's whole side of a call — signing, the TwiML exchange,
the media WebSocket, the ceiling — against a real instance. Result on this box:

| Check | Result |
|---|---|
| `T-16` signature → TwiML → `call_id` + admission token | ✅ all four |
| `T-05` unsigned webhook | ✅ 403, empty body |
| `T-06` withheld number | ✅ stream issued |
| `T-01` the round trip: synthesised speech → STT → answer → µ-law back | ✅ 500 ms of answer audio, **0 ms** to first audio with warm models |
| `T-12` ceiling reached, refused call gets busy TwiML and no stream | ✅ (effective ceiling **2**) |
| `T-13` dropped stream releases its slot | ✅ next call admitted |
| §8.6 tokenless stream naming a live call | ✅ closed **1008** |

**New `--local [port]` mode.** The default still drives the public tunnel; `--local` sends the HTTP
POST and the WebSocket to loopback while signing for the instance's configured public URL. The
signature covers a URL *string*, so the webhook validates it identically — what local mode does not
exercise is TLS and the tunnel hop. It exists so the acceptance can run on a box that must not be
exposed to the internet.

**Two operational findings, both from running it rather than reading it:**

1. **The VRAM ladder is live now, and it works — including when the box is full.** On the first run
   every call came back as busy TwiML: with the GPU enabled, `CapacityMonitor` starts sampling and
   had walked the ladder to `voice_shed` → `stt_off` → `commercial_only`. It was right to: Ollama
   was holding **13.4 GB resident "Forever"** (`qwen2.5:14b` alone is 10 GB), leaving 1.4 GB against
   a configured sibling floor of 6 GB. Unloading the idle models (`ollama stop`, a cache eviction —
   they reload on demand) took free VRAM to 14.6 GB and the ladder climbed back to `full`, one rung
   per sample with hysteresis. **This is US-018's first real execution** — its runtime trigger had
   never fired on this box because the monitor only starts where there is a device.
2. **An admitted call with no stream holds its slot for up to an hour.** The webhook takes the slot
   and the relay releases it; a call that is admitted and never streams (a caller who hangs up in
   between, a probe) keeps it until `release_stale` sweeps at 3600 s. With a two-slot ceiling, two
   such calls refuse everyone — which is what the first two runs of this harness did to themselves.
   The harness now gives back every slot it borrows. **Not yet addressed in the product**: a
   short-lived reaper for "admitted but no stream" would be a genuine improvement.

**Run over the public tunnel as well** (2026-10-04, `cloudflared` quick tunnel → `wss://`): a cold
instance scored **14/14** with **7459 ms** to first audio, and the warm re-run **15/15** with
**0 ms**. Two things follow. The T-01/T-17 budget (p95 first-audio ≤ 1.2 s) is met comfortably once
the models are warm, and *not* met on the first call after a cold start — the load dominates that
turn, so a load test must warm the instance first or it will measure Whisper's start-up, not the
product.

**What is still missing is only what needs a carrier**: the handset half of `T-01…T-17`, a real
34-character CallSid, and the timing budgets on a real network. The steps are in
`platform/docs/twilio-voice-setup.md`; the tunnel is the one part that needs your decision — it
opens this box (Twilio credentials, admin endpoints) to the internet, so it should be up only for
the duration of a test.

**Every defect in the sixth pass's register (§8.1–§8.29) is resolved, and every document that
contradicted the code has been rewritten. The seventh pass added seven (§8.30–§8.36), and all seven
are now fixed** — US-017 restored, the WS gate made whole, and the five follow-ups closed. What
remains is one acceptance run, two blocked builds, and three questions that need a person.

| Batch | Stories | Done when |
|---|---|---|
| **0. Foundations** ✅ | **US-025** trace · **US-027** frozen eval set · release-gate fixes (§8.5, §8.19) — *all done 2026-10-04* | Traces emit ✅ · the gate runs the frozen set with a pinned hash ✅ · an absent instance reads as `[NOT RUN] … not a product failure` ✅ · tooling scores the deployed environment ✅ |
| **1. Live fixes** ✅ | **US-017** barge-in · **US-022** handoff + notification (§8.1/8.2) — *both done 2026-10-04* | Interrupting stops the remaining clauses, sends `clear` once and marks the transcript ✅; a second escalation opens a second handoff, a notification is attempted and recorded, and the open queue is on the console ✅ |
| **2. Voice core** ✅ | **US-018** admission · **US-014 + US-015** LLM prose + breaker — *all done 2026-10-04* | The ladder has a runtime trigger and a defensible CPU-host policy ✅; open questions use grounded prose, commercial intents never reach the model, a dead model degrades prose only, and the breaker's state is on every trace ✅ |
| **3. Carrier proof** | **US-001** | `T-01…T-17` pass; a real CallSid lands in `inbound_event`. The code half is **done**: the WS-admission gate is whole (§8.6 ✅, completed by §8.31 — a stream that never sends `start` is refused), US-017's barge-in trigger is wired (§8.30 ✅), and `scripts/carrier_acceptance.py` drives Twilio's side of T-01/T-05/T-06/T-12/T-13/T-16 without a carrier. What remains is the handset half + the real call, which also confirms §8.30's thresholds on a real echo path — steps in `platform/docs/twilio-voice-setup.md` |
| **4. Channels** ✅ | **US-002** WhatsApp · **US-003 + US-008** browser mic — *all done 2026-10-04* | UC-02 and UC-04 are driven by `system_check` and their deferrals are removed ✅ · ten redeliveries produce one reply ✅ · the outbox retries on schedule and dead-letters permanently ✅ · a forged Twilio signature and a bad HMAC are 403/401 with no side effects ✅ · the browser client speaks the frozen frame contract and every failure lands in DEGRADED with text one tap away ✅ · live: 4/4 voice e2e (desktop + mobile) on a fresh instance, session rows close with a reason ✅ |
| **5. State** ✅ | **US-006** transcripts · **US-004** identity (SQLite behind a Protocol — CR-02 ordering constraint) — *both done 2026-10-04* | Transcripts survive restart with contiguous `seq` and no interleaving ✅ · the review model pages, labels expiry and renders handoffs ✅ · a phone typed on web chat links to the lead a call from that number resolves through, and the product carries ✅ · ten duplicate starts → one conversation ✅ · a stale thread yields a child, never a silent reopen ✅ |
| **6. Retrieval** ✅ | **US-010** index lifecycle · **US-012** provenance — *built 2026-10-04; US-010 re-opened and fixed the same day* | Promotion is a pointer swap that survives restart ✅ · boot refuses an empty index ✅ · a failing validation refuses promotion ✅ · **the rebuild embeds into — and validates — the collection it promotes** ✅ (§8.23: a rebuild promoted `anita_ams__v5` with 141 vectors embedded and validated, and a fresh process serves it) · replies carry chunk-level citations and the console review renders them ✅ · "not published" is answered from the probe with 0 LLM calls ✅. `corpus.caveat_violations()` at boot remains the one open nicety (US-013) |
| **7. Defect sweep** ✅ | **§8.10** arity · **§8.24** citation verdict · **§8.25** voice queue · **§8.26** outbox rung · **§8.27** probe · **§8.28** dead code — *all done 2026-10-04* | The refusal-asset path records instead of raising ✅ · a non-zero `uncited`/`incomplete` count refuses promotion and travels into the report ✅ · voice turns take the conversation slot, reproduced as a race ✅ · the backoff ladder and the docs match the code ✅ · the extra-channel probe runs at boot and refuses a channel with no credential ✅ · every dead symbol is called or removed — retention and summary retry are now janitor duties, and six unused symbols are gone ✅ |

**Hard dependencies:** US-027 → US-010 and every live-wiring story (the eval set is the release
evidence); US-025 → US-001 T-17 and US-017 (budgets read from traces); US-017 + US-018 → US-001;
US-006 → US-004; US-014 → US-015.

**Each story closes with:** tests green · `system_check` with no new deferrals · release gate against
a running instance · the evidence line recorded here · the UC deferral removed **last**.

### Still blocked, still deferred

| Item | Blocked on |
|---|---|
| US-021 outbound worker | DG-08 legal review — the queue is built and waits |
| CR-02 Postgres + Docker | Q9–Q12 (§5) |
| U-1 / U-2 / U-3 | Open product questions (§6) |

#### The GPU, enabled 2026-10-04

`torch 2.14.1+cu130` was installed, and the box went from *believing* it was a CPU host to using the
card. Verified rather than assumed — the numbers below are measured, and two of them initially said
the opposite:

| Check | Before | After |
|---|---|---|
| `detect_platform()` | `cpu` / float32 | **`cuda` / int8 / 15.9 GB / nvidia** |
| CUDA capability | — | **sm_120** (Blackwell; a build without those kernels would have failed at the first launch) |
| Effective call ceiling | 1 (`CPU_CONCURRENT_CALLS`) | **2** (the configured value) |
| US-018 VRAM monitor | never started | runs (nvidia-smi sampling) |
| Kokoro TTS | CPU, RTF 0.63 | **CUDA provider live, RTF 0.084** (~7.5×) |
| faster-whisper STT | CPU | **cuda/int8, RTF 0.002** |
| Boot degradations | 3 | **1** (SMTP only) |

**Three things had to be true at once, and the code only knew about the first:**

1. `torch` from the **cu130** index — the plain PyPI wheel is a CPU-only build, and ONNX Runtime's
   CUDA provider wants cuDNN 9 + CUDA 13 (`voice.py` records an earlier attempt that failed here).
2. **`onnxruntime-gpu`** instead of `onnxruntime`. The CPU wheel lists no `CUDAExecutionProvider`,
   so the session landed on CPU no matter what torch provided.
3. **CUDA 12's cuBLAS, alongside** (`nvidia-cublas-cu12`). CTranslate2 is built against CUDA 12 and
   loads `cublas64_12.dll`; a cu130 torch ships only `_13`. The failure was *late and quiet* — the
   model constructed and logged `ready on cuda`, then raised `RuntimeError: Library cublas64_12.dll
   is not found or cannot be loaded` on the first transcription, i.e. on a live call's first
   utterance. `voice_platform.ensure_ctranslate2_cuda_libs()` now registers that directory, and it
   had to do so **on `PATH` as well as via `os.add_dll_directory`**: with the directory merely
   added, ctypes could load the DLL but CTranslate2's own C++ loader still could not (measured).

**Two harness defects surfaced on the way**, both of which had been hiding the third problem:
`scripts/voice_e2e.py` pinned its STT round trip to `device="cpu"` (so it reported a green STT on a
box whose live path was broken) and crashed on a `→` under a cp1252 console. It now round-trips on
the configured device, names it in the output, and treats a failure there as `FAIL` rather than
`SKIP`.

**`T-17` is unblocked**: the load test is specified against a 2-slot ceiling, which the box now has.

**Deferrals, verified against `config/anita.yaml` on the sixth pass: `UC-09` is the only one.** The
UC-02/UC-03/UC-04/UC-14 entries were removed when their routes landed; UC-14 is not deferred but
*unconfigured* (`system_check` reports `NOT CONFIGURED`).

### Housekeeping (parallel, any time)

**Done in Batch 7:** the dead-code sweep (§8.12, §8.28) and the Twilio runbook's stale "What is
still missing" section (§8.15).

**Also done:** the four stale documents (§8.13, §8.14, §8.16, §8.17) and the config audit's
`PENDING` bucket (§8.29).

**Done 2026-10-04 (housekeeping pass):** `data/chroma_us103_probe/` and `data/chroma_anita/` are
git-ignored as the runtime artefacts they are, and **`ANITA_SESSION_SECRET` is set** in
`platform/.env` — generated, not echoed — so browser-mic and stream tokens now survive a restart.
The boot report's degradation list went from three to two.

Still open — one needs a value only you have, the rest is code:

- **SMTP.** `SMTP_*` + `SALES_NOTIFY_EMAIL` are still unset, so a handoff is recorded and stays
  `open` in the admin queue rather than emailing the rep (`GET /api/admin/handoffs` and the
  console's Escalations panel show it either way). Any SMTP credentials will do — this is a value,
  not code.
- ~~**A GPU that the app is not using.**~~ ✅ **Done 2026-10-04.** This machine has an **NVIDIA
  RTX 5060 Ti, 16 GB** and was running as a CPU host because `torch` was absent. It now runs on the
  GPU — see the note below for what that took and what it changed.
- ~~**Two tracked runtime artefacts (§8.36).**~~ ✅ **Done 2026-10-10.** `platform/logs/anita_crm.sqlite`
  and `platform/logs/anita/preflight.json` are `git rm --cached` and covered by the `logs/` rule
  `platform/.gitignore` already carried, so the tree no longer dirties on every run and no committed
  report contradicts the code. `platform/README.md`'s stale test count is corrected (1156 → 1194).
- **§10 — the university-domain cleanup tasks**: all CL-1…CL-11 done; nothing outstanding.

**Commit state (seventh pass, 2026-10-10):** every code change described in this file is committed
— the US-001 build log (`8402833`, `d053304`, `f380d97`, `382622e`, `a04771f`, `dfcb6c4`, `4fe32b0`,
`6d06229`), then the audit (`1f18ff8`), the staging-collection fix (`bded3be`), the WS-admission gate
and acceptance harness (`03dc8b9`), the Batch 7 sweep (`7be50e9`), the pending-list refresh
(`2413b19`), the stale-document and config-audit fixes (`a81f54e`), housekeeping (`75048e7`), the GPU
enablement (`3ffc8dc`), the carrier acceptance and its local mode (`26b10cd`), the public-tunnel
record (`231e155`), and the chat.html ticket fix (`085b68f`) — **the sixth pass's list stopped at
`7be50e9` and was six commits stale, which is why it is written out in full here.** The last commit
is `085b68f` (2026-10-04).

**Uncommitted at the time of writing (seventh pass, 2026-10-10):** the §8.30–§8.36 fixes and their
31 tests (`voice.py`, `media_stream.py`, `browser_voice.py`, `voice_webhook.py`, `orchestrator.py`,
`observability.py`, `boot.py`, `rebuild_index.py`, `release_gate.py`, and six test files), the
untracking of the two runtime artefacts, `platform/README.md`'s count, and three SDLC documents.
`git status` is otherwise **clean** — for the first time in this build, because §8.36 removed the
runtime database that used to show as modified after every run.

**Working tree otherwise:** `docs/sdlc/status-report.html` — the stakeholder-facing status page, a
derived artefact, still untracked and now stale against this file. The branch is still named
`chore/secret-hygiene-and-config-audit`; if this becomes a PR it should be renamed.

---

## 10. University-domain cleanup — task list

**Status 2026-10-04: CL-1 … CL-11 are all done.** The engine speaks, corrects, prompts, logs and
scrubs in ANITA's vocabulary; `grep -n "app\." app/anita/voice.py` returns nothing; and two guards
now keep it that way — a tree-wide vocabulary net (CL-10) and a vendored faithful-copy manifest
(CL-11) that cannot silently skip.

**Research basis:** a full-tree scan (2026-10-04) found **110 in-scope occurrences across ~24
files**, in three kinds: deliberate isolation guards, provenance prose, and live leftovers. The
corpus is clean (0 blocked markers in 141 chunks); the isolation guards are correct and complete.

**Bottom line: exactly one production file carries the source product's domain on a live path —
`platform/app/anita/voice.py`.** Everything else is either a rejection guard (keep), provenance
(the migration audit trail — keep), or a naming leak (rename).

**Constraint that shapes every task below.** `tests/test_mod05_voice_migration.py::test_the_engine_is_a_faithful_copy`
diffs the engine's `def`/`class` lines against `D:/project/universityDemo/app/voice_handler.py` and
requires `added == DELIBERATE_NEW` exactly. **Function bodies and class attributes may be edited
freely; adding or removing a function requires updating that list in the same change.** Removing
definitions is therefore avoided below — repurpose in place wherever possible.

### 10.1 Live — a caller hears or is affected by this today

* [x] **CL-1 (highest) The spoken closing line.** ✅ **Done 2026-10-04.**
  `voice.py:615` now reads `CLOSING_REPLY = "Thank you for calling ANITA Medical Systems. Have a
  great day!"`, spoken deterministically on any sign-off (`voice.py:602-618`, `1302`).
  **Evidence:** `pytest tests/test_mod05_voice_migration.py -q` → **41 passed**;
  `test_a_sign_off_speaks_the_anita_closing_line` drives `process_utterance` with stubbed STT/TTS and
  asserts `end_call is True`, the ANITA line in the dialogue, and no sibling term.
  *Residual:* a media-stream call re-proves it in Batch 3 — the simulated-call client is not yet a
  repo script, so the engine path is the repeatable evidence today.
* [x] **CL-2 STT corrections.** ✅ **Done 2026-10-04.**
  `voice.py:1433` `_CORRECTIONS` now holds twelve ANITA catalogue corrections (`x ray`→`X-Ray`,
  `ultra sound`→`Ultrasound`, `fuji film`→`FUJIFILM`, `f c r`→`FCR`, `capsule a`→`Capsula`,
  `pro fect`→`Profect`, `micro bar`→`Microbar`, `intensive flying`→`Intensifying`,
  `cath-lab`→`Cath Lab`, `annita`→`ANITA`). Matching is now **word-boundary + escaped**
  (`voice.py:1448`) — the old substring substitution could fire inside a longer word.
  The anti-rot invariant is asserted, not promised:
  `test_every_correction_target_is_catalogue_vocabulary` checks every target against
  `data/out/ams.sqlite` names/brands + the company identity, and
  `test_no_correction_carries_sibling_vocabulary` blocks the old table from returning.
  **Evidence:** `pytest tests -q` → **856 passed, 1 skipped** (was 850); the mis-hearing test turns
  *"how much is the ultra sound gel and the ex ray cassette"* into *"…Ultrasound…X-Ray…"*.
  *Residual:* the STT leg itself (Whisper) is re-driven by the Batch 3 carrier proof.
* [x] **CL-3 Clarification vocabulary.** ✅ **Done 2026-10-04.**
  `voice.py:372` `_CLARIFICATION_RE` now matches `product|model|item|variant|version|one|size|pack`
  and `quantity` in place of `program|course|intake|semester|term` / `fall,? ?spring`; the
  domain-neutral lead-ins (`could you specify`, `did you mean`, `just to confirm`,
  `are you looking/asking/…`) are deliberately kept — they are the actual loop-breaker signal.
  **Evidence:** `test_an_anita_clarification_reads_as_one` (six ANITA questions match) and
  `test_admissions_phrasing_no_longer_reads_as_a_clarification` (five admissions questions do not) —
  *"Which intake are you applying for?"* → `False`, *"Which specific product are you asking
  about?"* → `True`. `pytest tests/test_mod05_voice_migration.py -q` → **48 passed**.
* [x] **CL-4 Meta-leak scrubber.** ✅ **Done 2026-10-04.**
  `voice.py:560` `_META_MARKERS` now carries `"catalogue profile"` in place of `"university
  profile"`; the `_PREFIX_STRIP` docstring (`voice.py:571-574`) and the sentence-splitter example
  (`voice.py:644`, now `₹2,500. Shipping extra`) use catalogue examples. The generic leak markers
  (`based on`, `let's assume`, `the caller`, …) are unchanged.
  **Evidence:** `test_the_scrubber_strips_the_meta_prefix_and_keeps_the_answer` — *"Here's the
  relevant information from the catalogue profile: Ultrasound Gel is listed at ₹60/Piece."* →
  *"Ultrasound Gel is listed at ₹60/Piece"*; a pure-meta sentence returns `None`; clean text passes
  through byte-identical; the scrubber carries no sibling term.
  *Residual:* "no sibling string in the module" is not yet true — the prompt builder (CL-5) still
  holds the admissions strings, and `voice.py` line numbers after this task shift again when it lands.

### 10.2 Latent — built every turn, discarded today, live the moment US-014 lands

* [x] **CL-5 The prompt builder.** ✅ **Done 2026-10-04.**
  `_build_llm_prompt` (`voice.py:1658`) now builds the catalogue assistant: *"Answer naturally as a
  catalogue assistant for medical equipment"* (`voice.py:1674`), *"concrete information from the
  catalogue"* (`voice.py:1679`), and in the loop-breaker branch *"concrete product information from
  the catalogue — model, specifications, price"* (`voice.py:1706`). The docstring records why it is
  inert today and that US-014 makes it live.
  **Evidence:** `test_the_prompt_names_the_catalogue` and
  `test_the_loop_breaker_prompt_names_the_catalogue` (the two-clarifications-in-a-row branch) — both
  assert catalogue vocabulary, the caller's words, and no sibling term. Faithful-copy guard green.

### 10.3 Naming and provenance

* [x] **CL-6 Logger names.** ✅ **Done 2026-10-04.**
  `voice.py:46` → `anita.voice`; `voice.py:51` → `anita.voice.events`;
  `shared/sentiment/core.py:39` → `shared.sentiment`. No log-scraping reference to the old names
  existed in `docs/`, `scripts/` or `*.ps1`, so nothing else needed updating.
  **Evidence:** `test_the_loggers_are_named_under_anita`,
  `test_no_logger_is_named_after_the_source_module` (source scan), and
  `test_the_package_logs_under_its_own_name` for the shared package.
* [x] **CL-7 Stale source references.** ✅ **Done 2026-10-04.**
  The module docstring now names what the code actually uses — faster-whisper, `anita.voice_pipeline`
  / `anita.voice_rag`, `kokoro_onnx` — and its usage example shows the real entry points
  (`feed_audio` → `process_utterance`) instead of the `handle_media` that never existed. The STT,
  cancel-chain and trace comments name anita modules.
  **Evidence:** `grep -n "app\." app/anita/voice.py` → **no matches at all**, not merely
  provenance-only. Deliberate provenance notes elsewhere (`voice_settings.py:3`, `anita/__init__.py`,
  `media_stream.py:4`, `audio_format.py:19`, `speech.py:4`) are kept.
* [x] **CL-8 Dead copied machinery.** ✅ **Done 2026-10-04.**
  `generate_ulaw_greeting` removed — it pre-synthesised the source's WebSocket greeting, ANITA greets
  through TwiML `<Say>`, and nothing in this repo called it. A NOTE at `voice.py:341` records the
  removal, and the faithful-copy test lists it in `DELIBERATE_GONE` in the same change.
  **Evidence:** `test_the_engine_is_a_faithful_copy` passes with the two-entry divergence list.
* [x] **CL-9 Product-agnostic package hygiene.** ✅ **Done 2026-10-04.**
  The discovery was worse than this task recorded: `shared/sentiment/core.py` held a **second**,
  industry-flavoured fallback (`EXTRACTION_PROMPT = _GENERIC_FALLBACK`, with the duplicated
  "supplier / supplier") that `build_extraction_prompt()` actually served, while the guarded
  constant `GENERIC_EXTRACTION_PROMPT` sat unused in `config.py`. The duplicate is deleted and the
  fallback is now the generic constant — one prompt, one source (`core.py:107-116`).
  **Evidence:** `test_the_fallback_prompt_is_the_generic_one` (asserts the *served* string, not the
  constant), `test_the_fallback_substitutes_the_message`, `test_the_package_logs_under_its_own_name`.
  **Defect this surfaced:** `tests/test_mod07_sentiment.py::test_prompt_carries_the_product_taxonomy`
  had been passing against that fallback — it was pinning ANITA's product taxonomy to a string ANITA
  does not serve. It now checks `anita_sentiment_config().extraction_prompt`.

### 10.4 Make it impossible to come back

* [x] **CL-10 The regression net.** ✅ **Done 2026-10-04.**
  `tests/test_vocabulary_isolation.py` scans `platform/app/anita/**`, `shared/**`, `platform/scripts/**`
  and `streamlit_app.py` and fails on 22 sibling terms (word-boundary matched).
  **Design:** only *code* is checked — non-docstring string literals plus function/class/variable
  names. Docstrings and comments are exempt on purpose: that is where the migration's audit trail
  lives, and prose cannot reach a caller. Three files are exempt — `config.py`, `index.py`,
  `stores.py` — each with its reason recorded, and `test_every_exemption_is_earned` fails if an
  exempt file stops containing the vocabulary it is exempted for.
  **Evidence:** the net caught this change set's own `scripts/voice_manifest.py` (it hardcoded the
  sibling source path) — fixed by reading the path from the manifest instead of exempting the script.
  A planted-string probe (`_pollution_probe.py` with the old closing line) makes it fail with all
  three terms named; removing the probe makes it pass. `test_the_scanner_finds_planted_terms` keeps
  the scanner itself honest, and `test_the_scan_covers_the_product_tree` fails if a scan root moves.
* [x] **CL-11 Guard the guard.** ✅ **Done 2026-10-04.**
  The faithful-copy check now reads vendored evidence — `tests/fixtures/voice_engine_manifest.json`
  (source SHA-256, line count, and all 54 source definitions), written by
  `scripts/voice_manifest.py`. It sits under `fixtures/`, not `data/`, because `data/` is
  gitignored as a runtime artefact and this file is source. **The skip is gone**: a missing or empty manifest *fails*
  (`test_the_manifest_is_present`), so on a clean checkout or in CI the engine is checked, not
  silently skipped. When the sibling repository *is* present and has moved on, the test warns that
  the manifest is stale rather than failing — the sibling is a different product and is free to
  evolve; the copy is still compared against the pinned manifest.
  **Evidence:** Probe A — manifest moved aside → 2 tests **fail** (previously `pytest.skip`).
  Probe B — manifest repointed at a differing file → the freshness `UserWarning` fires.
  Restored: 57 tests pass in the two affected files.

### 10.5 Not pollution — do not "clean"

| Item | Why it stays |
|---|---|
| `config.py:120-135,158-159`, `index.py:76`, `isolation.py`, `stores.py`, `errors.py`, `config/anita.yaml` (`isolation_forbidden`, `corpus_gate.blocked_markers`) | Rejection lists — they exist so a copy-pasted sibling config is refused at boot. Removing them removes the isolation |
| `tests/*` sibling markers and negative fixtures (`meridian_kb__v1`, `"Meridian University admissions requirements"`) | The guard tests and their fixtures; they assert the refusal works |
| Provenance prose in `shared/rag`, `shared/sentiment`, `voice_settings.py`, `anita/__init__.py`, `platform/README.md`, `.env.example` | The migration audit trail: where the code came from and what was retargeted |
| `docs/sdlc/**` references to `universityDemo` / Meridian | Historical planning and reconciliation records |
| `admission` (singular) throughout `speech.py`, `voice_webhook.py`, docs | **Call admission** — capacity control, unrelated to university admissions |

### 10.6 Order and evidence

**All eleven tasks done 2026-10-04**, in the recorded order CL-1 → … → CL-11. Totals for the change
set: `pytest tests -q` → **875 passed, 1 skipped**; `scripts/system_check.py` → **16 passed ·
0 failed · 5 deferred**; per-task evidence in each entry above. CL-5 landed ahead of US-014, the
deadline it carried. CL-10/CL-11 change no product behaviour — they exist so CL-1…CL-9 cannot
silently regress.

**The net is live:** any reintroduction of sibling vocabulary in product code fails the suite with
the file, line and term named; the faithful-copy check **fails rather than skips** when its manifest
is missing, so a clean checkout leaves the engine guarded.
