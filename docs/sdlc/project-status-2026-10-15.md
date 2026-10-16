> **Lens:** independent verification pass · **Date:** 2026-10-15 · **Tree verified:** commit `3f7a916`, branch `chore/secret-hygiene-and-config-audit`
> **Scope:** the whole repository — the data-collection pipeline, the `platform/` product, the SDLC plan set, and every open item.
> **Relationship to the other documents:** this is a *fresh* pass, not a rewrite. `work-status.md` is the build's own seven-pass record; this file re-runs its numbers and re-traces its claims on today's tree, and reports what that finds. Where the two disagree, §5 says so.

# Project Status — ANITA MEDICAL SYSTEMS

**Bottom line:** the engineering is finished. Of 41 user stories, **38 are `COMPLETED`**; the three
that are not are held by things outside the software — one needs a telephone, one needs a lawyer,
one needs four decisions. Today's re-run reproduces every headline number in the build's own record
exactly (1194 unit tests, 70/70 quality cases, 20/0/0·1 system check, 141-chunk corpus). No open
engineering defect was found. One **repository-hygiene defect the build's register does not carry**
was found and is reported in §5.1.

---

## 0. What this project is

Two halves in one repository, joined by a directory (`data/out/`) rather than by code:

| Half | Where | What it is | Language |
|---|---|---|---|
| **A — Data collection** | `src/ams/`, `scripts/`, `config/site.yaml`, `data/` | Crawls anitamedicalsystems.com once and emits the corpus in six RAG-consumable families (chunks, Q&A, graph, tables/SQLite, media manifest, API inventory) | ~6.3k lines Python |
| **B — The product** | `platform/` | A multi-channel assistant that answers from that corpus: web chat, PSTN voice, WhatsApp, browser-mic voice, operator console | ~20.1k lines app + ~13.8k lines tests |
| **Shared** | `shared/rag/`, `shared/sentiment/` | Product-agnostic packages extracted in CR-01; neither imports `anita.*` or `app.*` | ~2.3k lines Python |

The platform was re-homed from a sibling product (a university admissions assistant). That origin is
why a large part of the design is about **isolation** — refusing to boot if this instance points at
the sibling's database, vector collection, port or log path.

Totals: **431 files tracked — of which 194 are one test tool's vendored `node_modules`** (see §5.1).

---

## 1. How this report was produced

Not by reading the status documents and repeating them. Three independent methods:

1. **Live re-run.** Every headline figure in `work-status.md` §7 was re-executed on today's tree,
   with the platform's own virtualenv. §2 is that table.
2. **Code trace of the negative claims.** A claim that something is *not* built cannot be checked by
   reading a docstring, so the three "pending/blocked" claims were traced to call sites: does
   anything outside a test file construct the symbol, serve the route, or dial the number? §4 says
   what that found. **All three held.**
3. **Sweep for the unrecorded.** A search for markers the build's own register might have missed —
   supply-chain artefacts, generated files, stale counts, `TODO`/`NotImplementedError` in product
   code. §5 is what it found.

The build has already audited itself seven times (the seven passes in `work-status.md`), each pass
finding and fixing real defects. This pass is not a rebuttal of that work; it is a check on it.

**What this pass could not verify.** Anything needing a carrier or a browser: the handset half of
`T-01…T-17`, the two Playwright suites (54 scenarios), and the live-Ollama prose timing. Those are
recorded as *carried* evidence below, not as re-run evidence, and are labelled as such.

---

## 2. Live verification — re-run 2026-10-15

| # | Check | Command (from `platform/`) | Result today | Vs. the record |
|---|---|---|---|---|
| 1 | Unit suite | `.venv/Scripts/python -m pytest tests -q` | **1194 passed, 1 skipped**, 0 failed, 114 s | ✅ identical |
| 2 | Pipeline suite | `../.venv/Scripts/python -m pytest tests -q` (repo root) | **51 passed**, 0.4 s | ✅ green |
| 3 | System integrity | `.venv/Scripts/python scripts/system_check.py` | **20 passed · 0 failed · 0 warnings · 1 deferred** | ⬆️ was 19 — see below |
| 4 | Quality gate | `.venv/Scripts/python scripts/release_gate.py --no-browser` | **70/70 cases**, 55 grounded, **0 uncited, 0 incomplete** | ✅ identical |
| 5 | Boot preflight | `PYTHONPATH=app .venv/Scripts/python -m anita.boot --preflight` | **OK** — 43 keys live · 5 pending; 141 chunks; collection `anita_ams__v5`; `device=cuda` (RTX 5060 Ti); one degradation: SMTP | ✅ identical |
| 6 | Eval-set pin | SHA-256 of `eval/anita_frozen_set.jsonl` | `eb2ffc27c88cf18ed872d042b2f93b6fdae20efe3f7911dfd820a2e9173164dd` | ✅ matches `config/anita.yaml:183` |
| 7 | Working tree | `git status --short` | clean, **1 untracked file** (`docs/sdlc/status-report.html`) | ✅ no runtime dirt |

**Two results need a word of explanation, because a raw table would mislead:**

* **Check 3 is 20 today, not the 19 the record shows.** The one difference is `UC-04` (browser-mic
  voice). On 2026-10-10 the record shows it as `NOT TRIGGERABLE HERE — voice shed at level
  voice_shed`: the sibling product's Ollama models were resident on the shared GPU, leaving 446 MiB
  free against a configured 6 000 MiB sibling floor, so the capacity ladder had correctly shed
  voice. Today the card has room and the ladder is back at `full`, so UC-04 passes. **This is the
  VRAM ladder working, not a regression in either direction** — but it is also a standing property
  of this deployment: *the assistant's voice capability depends on what the sibling product is doing
  with the shared GPU.* Worth knowing before a demo.
* **Check 4 ends in `RESULT: FAIL — promotion refused`.** That is the gate behaving correctly, not a
  product failure: with `--no-browser` the browser suite never ran, and the gate's own rule is that
  *an unrun suite is not evidence*. The 70/70 evaluation set and the citation counts — the parts
  that did run — both passed. A promotion run needs an instance and Playwright.

**Carried evidence (not re-run today), from the build's record:** carrier-free acceptance 15/15 over
the public tunnel (14/14 cold, 7 459 ms to first audio cold vs 0 ms warm); browser suites 50/50 and
4/4 on a frozen tree; a 48-turn transcript surviving restart with contiguous `seq`; cross-channel
identity confirmed chat→voice.

---

## 3. What is done

### 3.1 The SDLC plan — Stages 0–11 complete

Every stage artifact exists and is self-consistent: intent (AS-01…AS-10), BRD (BRD-01…BRD-23,
Discovery Matrix 13/13), use cases (UC-01…UC-15, 14/14 scenarios each), data & state (DAT-01…16,
DG-01…13, SM-01…06), coverage gate (COMPLETE, 3 iterations), modularization (MOD-01…MOD-10),
architecture, brownfield reconciliation (REC-01…REC-23), module TRDs (TRD-01…TRD-48), 30 stories
with HLD/LLD, coverage verification (19/19 capabilities), and a verification scorecard at 100 %.

**Qualification that the plan itself makes and this pass repeats:** the scorecard measures *plan*
completeness, not product readiness. A `PASS` there is not a launch claim.

### 3.2 The ten modules — all built

`platform/app/anita/` holds **50 Python files, 20 120 lines**. All ten modules are implemented and
reachable from a served surface (`system_check`: **47/49 modules reachable, 11 dependencies wired,
none absent**):

| Module | What it does | Evidence it runs |
|---|---|---|
| MOD-01 Channel Gateway | Twilio webhook + media relay, WhatsApp, browser mic, extra-channel registry, send outbox | routes served; replay + attribution tables; UC-02/03 driven |
| MOD-02 Conversation Orchestrator | identity precedence, classification, turn serialisation, transcripts | 644 conversations live in the store |
| MOD-03 Retrieval & Grounding | BM25 + weighted RRF + grounding gate, Chroma, index lifecycle | 141 vectors; `anita_ams__v5` serving |
| MOD-04 Inference & Answer Composition | deterministic price/MOQ/delivery with caveat; LLM prose for open questions only | zero model calls on a commercial question |
| MOD-05 Speech Services | STT/VAD/end-of-speech, clause TTS + barge-in, VRAM ladder | Kokoro RTF 0.084 on CUDA; ladder samples the device |
| MOD-06 Lead & Mock CRM | capture + dedupe, `CrmStore` Protocol, handoffs, rep notification | handoffs on the admin queue |
| MOD-07 Sentiment & Escalation | rule-based scoring, composite category escalation to a real sink | escalation recorded at score −1.00 |
| MOD-08 Observability & Evaluation | per-turn traces, admin read API, frozen eval set + release gate | 70-case set, hash-pinned |
| MOD-09 Config, Isolation & Boot | typed config, read-coverage audit, collision guard, secrets, preflight | refuses a copy-pasted sibling config, exit 2 |
| MOD-10 Client Surfaces | `chat.html`, `console.html`, `pcm-worklet.js`, Streamlit | served at `GET /` and `GET /console` |

### 3.3 The stories — 38 of 41 complete

| Group | Complete | Not complete |
|---|---|---|
| D4 build, US-001…US-030 | **28** | US-001 (`IN PROGRESS`), US-021 (`BLOCKED`) |
| CR-01, US-101…US-115 | **10 / 10** | — |
| **Total** | **38** | 3 items (§4) |

The product's own `system_check` drives 13 of the 15 use cases to a passing result today; UC-09 is
deferred by decision and UC-14 is `NOT CONFIGURED` (no extra channel is enabled on this instance).

### 3.4 The data-collection pipeline — done and feeding the product

Five stages (`01_discover → 02_fetch → 03_parse → 04_emit → 05_validate`), 26 modules, **32
canonical page documents**, and a validation report at **Overall PASS, 10/10 gates** (coverage,
product completeness, shared-site block, chrome leakage, text corruption, extraction budget, graph
integrity, API inventory, SQLite lookups, determinism).

The outputs are on disk and real: `chunks.jsonl` **141**, `qa_pairs.jsonl` 164,
`voice_short_answers.jsonl` 164, `gaps.jsonl` 6, `graph_nodes.jsonl` 120 / `graph_edges.jsonl` 283,
10 normalised CSVs, `ams.sqlite`. The platform reads this corpus in place (`corpus.out_dir:
../data/out`), and a **corpus gate** refuses a corpus that does not carry ANITA markers or that
carries the sibling's — the 141-chunk figure is asserted in five tests, not just documented.

### 3.5 Cross-cutting properties that hold

These are the claims worth trusting, because each is enforced by code rather than by intention:

* **Prices cannot be invented.** Price/MOQ/delivery resolve from SQLite with the quotation caveat
  appended in the same function that produces the figure, and the model is never on that path.
* **Every grounded answer is cited.** 55 grounded answers, 0 uncited, 0 incomplete — and since
  §8.33 that count can **refuse an index promotion**, not just a release.
* **Absence is honest.** A product the catalogue stocks but has not documented is answered from a
  per-product coverage probe with **zero LLM calls**, instead of "we don't sell that".
* **Isolation is enforced at boot.** A copy-pasted sibling config is refused with exit code 2,
  naming the resource, both values and the fix — and a refused boot writes no report.
* **The copy cannot silently drift.** 22 sibling terms are blocked in product code by a regression
  net, and the voice engine is checked against a vendored definition manifest that **fails rather
  than skips** when absent.
* **No dead scaffolding.** A sweep for `TODO`/`FIXME`/`NotImplementedError` across `platform/app/`
  returns **nothing** — the only pattern hits are prose about phone-number formats.

---

## 4. What is pending

Everything still open, in one list. **None of it is an open engineering defect.**

### 4.1 One story in progress — and it needs a telephone, not code

| Item | State | What is actually missing |
|---|---|---|
| **US-001** — real carrier call | transport complete, exercised by a *simulated* call end to end over the public tunnel | A real handset call. No real 34-character Twilio CallSid exists anywhere in the logs or stores — a read-only scan finds 66 `inbound_event` rows, 61 of them synthetic ids minted by the acceptance harness. Outstanding: the handset half of `T-01…T-17`, the timing budgets on a real network, and the **barge-in thresholds on a real handset and echo path** (tuned values that only a real call can confirm) |

The gate work around it is done: signature → admission → TwiML ordering, an HMAC token per admitted
call, a relay that refuses a tokenless *or* `start`-less stream with close code 1008, and a
`status` callback that releases the admission slot **only on a terminal status**.

### 4.2 Blocked — waiting on decisions

| Item | Blocked on | Why it is right to hold |
|---|---|---|
| **US-021** outbound dialling worker | **DG-08** legal review | `OutboundQueue` is built (window and attempt limits checked at claim time, SKIP-LOCKED semantics, abandoned-row reaping) but **nothing constructs it outside tests** — verified again today by exhaustive search. Calling real buyers before a compliance review would be the wrong call |
| **CR-02** Postgres + Docker | decisions **Q9–Q12** | Verified again today: no `Dockerfile`, no compose file, no `Postgres*Store` anywhere; `psycopg2-binary` is declared in `requirements.txt:18` but **imported by nothing**; `DATABASE_URL`/`DB_*` exist only as *names* in the config audit's known-keys list. The four questions: which database, one server or two, loader-imports vs direct write, and where the loader lives. Recommendation on record: **PostgreSQL**, two logical databases on one server, a loader rather than direct writes |

### 4.3 Not started — waiting on people

| Item | Waiting on |
|---|---|
| **DG-08** legal review of the clinical-refusal wording | A lawyer. **Blocks public launch**, and blocks UC-09 |
| **U-1** "buyer declines a human" behaviour | A product decision — the decline intent does not exist in the evidence |
| **U-2** should `MIN_LABELED_OUTCOMES` be operator-tunable? | A product decision (currently a code default of 100) |
| **U-3** Side A's exact BANT completeness rule | Not present in the evidence |

### 4.4 Configuration values missing — settings, not development

| Item | Effect today |
|---|---|
| `SMTP_*` + `SALES_NOTIFY_EMAIL` | The **only** boot degradation now. A handoff is recorded and appears in the operator console and `GET /api/admin/handoffs`, but **no email reaches the rep** — it stays `open`. Honest, not broken: "we escalated" and "somebody knows" are kept as separate facts |
| A WhatsApp number | The WhatsApp path is built and driven end to end (signed inbound → answer → outbox row; ten redeliveries → one reply). No number is pointed at it, so no customer can reach it on that channel |
| An extra messaging channel | By design a config change, not code: `AdapterRegistry` declares → validates → **probes** → enables, and a channel declared without its credential refuses to enable at boot. None is enabled, so every extra-channel inbound 404s |

### 4.5 Residuals the build itself records (accepted, not hidden)

Worth carrying into any handover, because each is a real limit:

* **The first call after a restart is slow** — ~7.5 s to first audio while the speech models load;
  0 ms warm. A load test must warm the instance first or it measures Whisper's start-up.
* **The configured prose model is too slow for this host** — `qwen2.5:14b` does not answer inside the
  backend's 60 s timeout on this box; the live prose demo used `llama3.2:3b`. The prose leg is also
  **not covered by the frozen evaluation set** (which predates it).
* **An admitted call that never opens its stream holds its slot** until the 3 600 s sweep. With a
  two-slot ceiling, two such calls refuse everyone. The harness gives its slots back; the product
  could use a short-lived reaper.
* **Sentiment scoring is chat-only** — voice turns are never scored, and sentiment is not persisted,
  so a restarted conversation shows none in review.
* **Two things cannot be told apart from the review API** — whether the model was down (it is on the
  per-turn trace, not the turn row), and retrieval's `degraded` flag (dropped when the adapter
  rebuilds the result; it survives only in `status()`).
* **A promotion takes effect on a running process only at its next construction** — the pointer is
  read once at boot.
* **SQLite is shared through one connection** for reads and writes, so `TAC-7`'s "separate pool" is
  not claimed — recorded in the module docstring, and CR-02's to change.

---

## 5. Findings from this pass

### 5.1 🔴 Vendored dependencies and generated artefacts are tracked in git — **not in the register**

The largest single finding of this pass, and the one thing here that is genuinely new.

`platform/tests/e2e/` holds a Playwright install that is **committed to the repository**:

| Tracked path | Files | Size in HEAD |
|---|---|---|
| `platform/tests/e2e/node_modules/` | **194** | **17.7 MB** |
| `platform/tests/e2e/playwright-report/index.html` | 1 | generated test report |
| `platform/tests/e2e/test-results/.last-run.json` | 1 | generated run state |
| `platform/tests/e2e/catalogue-answer.png` | 1 | a screenshot artefact |

**194 of the repository's 431 tracked files — 45 % — are one test tool's dependencies.**
`package.json` and `package-lock.json` are tracked correctly, so the fix is a `.gitignore` rule plus
`git rm --cached`; nothing is lost.

**Why it matters, in the register's own terms.** §8.36 fixed exactly this class of defect for two
runtime files (`logs/anita_crm.sqlite`, `logs/anita/preflight.json`) and closed with "the tree no
longer dirties on every run". These four paths are the same defect, larger, and were missed. The
concrete costs: every checkout carries 17.7 MB it will immediately re-install; `playwright-report/
index.html` and `test-results/.last-run.json` **change on every test run**, so the tree dirties
again — the property §8.36 claimed to have restored; a vendored dependency tree does not receive
security updates and cannot be audited as a lockfile can.

**Not a product defect.** Nothing in the running product reads these paths. It is repository
hygiene, and it is cheap to fix: add `node_modules/`, `playwright-report/`, `test-results/` to
`platform/.gitignore`, then `git rm -r --cached` those paths.

### 5.2 Documentation drift found

| # | Where | What | Severity |
|---|---|---|---|
| a | `README.md:192` (root) | Says "**48 unit tests**". The suite collects and passes **51** (38 `def test_` plus parametrized cases) | Low — a count, not a claim |
| b | `README.md:61` (root) | Lists `url_manifest.json` under the `data/out/` outputs table. The file is at `data/url_manifest.json` | Low |
| c | `docs/sdlc/work-status.md` §7 | The `system_check` row reads 19 passed with UC-04 shed. Today it is 20 (see §2). A time-varying number recorded as a fixed one | Low — the doc explains the cause correctly |
| d | `docs/sdlc/status-report.html` | **Untracked and stale.** Dated 4 October, quotes 1 163 checks (now 1 194), and its "Partly" list still describes items that have since changed state. §D of `work-status.md` already flags it as stale | Medium — it is the stakeholder-facing page, so a stale copy is the one most likely to be read |
| e | Branch name | `chore/secret-hygiene-and-config-audit` no longer describes the work on it (the build's own record says so). If this becomes a PR, rename it | Low |

### 5.3 What this pass confirms about the build's honesty

Worth recording, because it is unusual and it is the reason the numbers above can be trusted: the
build's record is **self-critical in the right direction**. It demoted its own `COMPLETED` stories
twice (US-010, US-017) when tracing call sites showed the live path did not exist, and re-promoted
them only with a reproduction. Every claim this pass tested — including the three negative ones
that a docstring cannot prove — held against the code. The one figure that moved since the last
record moved because the *machine* changed, not because the record was wrong.

---

## 6. What would move this project forward

In the order that unblocks the most:

1. **Fix §5.1** — a `.gitignore` rule and `git rm --cached`. Ten minutes, no product risk, and it
   restores the clean-tree property §8.36 already claimed.
2. **Make one test call** (~5 minutes, with the tunnel up for the duration only). This is the single
   item holding US-001, and it is also the only way to confirm the barge-in thresholds on a real
   echo path. The steps are in `platform/docs/twilio-voice-setup.md`.
3. **Set `SMTP_*` + `SALES_NOTIFY_EMAIL`** — any credentials. Removes the last boot degradation and
   makes the rep-notification path real rather than recorded.
4. **Send the clinical-refusal wording to legal** (DG-08). Blocks public launch and UC-09.
5. **Answer Q9–Q12** (CR-02) if a scaled deployment is in scope; otherwise record that a
   single-file SQLite deployment is the accepted target for the pilot.
6. **Answer U-1/U-2/U-3** — three small product decisions.
7. **Refresh or delete `docs/sdlc/status-report.html`** — it is untracked and stale, and it is the
   page a non-engineer would read.

**Do not start anything else.** There is no engineering backlog: the seventh pass closed the last
seven defects the day it found them, and this pass found none. Work started now would be new scope,
not completion.

---

## Appendix — re-running every check in this report

```powershell
# root
cd D:\project\anitamedicalsystems
.\.venv\Scripts\python -m pytest tests -q                     # 51 passed

# platform
cd platform
.\.venv\Scripts\python -m pytest tests -q                     # 1194 passed, 1 skipped
.\.venv\Scripts\python scripts\system_check.py                # 20 · 0 · 0 · 1 deferred
.\.venv\Scripts\python scripts\release_gate.py --no-browser   # 70/70; promotion refused (no browser suite)
$env:PYTHONPATH="app"; .\.venv\Scripts\python -m anita.boot --preflight

# needs an instance and Playwright (not run in this pass)
.\.venv\Scripts\python scripts\release_gate.py
bash tests\e2e\run.sh ; bash tests\e2e\run_voice.sh

# needs a phone and a public tunnel (not run in this pass)
.\.venv\Scripts\python scripts\carrier_acceptance.py --local
```

**Source documents.** `docs/sdlc/work-status.md` (whole-of-work, seven passes, defect register
§8.1–§8.36) · `docs/sdlc/plan-state.md` (plan checkpoint) · `docs/sdlc/change-requests/STATUS.md`
(CR-01) · `docs/sdlc/FEASIBILITY-STUDY.md` (verdict: Feasible) · `platform/docs/twilio-voice-setup.md`
(the carrier-call runbook).
