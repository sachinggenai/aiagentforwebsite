# ANITA MEDICAL SYSTEMS — platform

Multi-channel product-marketing and support assistant for ANITA MEDICAL SYSTEMS:
web chat, Twilio voice (inbound and outbound), WhatsApp and browser-mic voice,
answering from the medical-equipment catalogue in `../data/out/`.

Re-homed from the Meridian University admissions assistant. Full plan in
[`../docs/sdlc/`](../docs/sdlc/); feasibility study in
[`../docs/sdlc/FEASIBILITY-STUDY.md`](../docs/sdlc/FEASIBILITY-STUDY.md).

---

## Status

| Module | Status |
|---|---|
| **MOD-09 Configuration, Isolation & Boot** | ✅ **built** — US-028, US-029, US-030 |
| **MOD-04 Deterministic answer path** | ✅ **built** — US-013 (TRD-18, TRD-19) |
| **MOD-03 Corpus access** | ✅ **built** — `corpus.py` (TRD-12 groundwork) |
| **MOD-03 Retrieval & Grounding** | ✅ **built** — `retrieval.py`: BM25, weighted RRF, **grounding gate** (TRD-13, TRD-14) |
| **MOD-03 Embeddings, store & index lifecycle** | ✅ **built** — `embeddings.py`, `stores.py`, `index.py` (TRD-12, TRD-15) |
| **MOD-04 LLM path, prompt assembly & grounding check** | ✅ **built** — `llm.py`, `prompts.py` (TRD-17, TRD-20, TRD-21) |
| **MOD-01 Channel Gateway** | ✅ **built** — US-001…US-003: Twilio webhook + media relay, WhatsApp, browser mic |
| **MOD-02 Conversation Orchestrator** | ✅ **built** — US-004…US-006: identity, classification, transcripts |
| **MOD-05 Speech Services** | ✅ **built** — US-016…US-018: STT/VAD, clause TTS + barge-in, admission ladder |
| **MOD-06 Lead & Mock CRM** | ✅ **built** — US-019…US-022: capture, dedupe, handoffs, notification |
| **MOD-07 Sentiment & Escalation** | ✅ **built** — US-023, US-024 |
| **MOD-08 Observability & Evaluation** | ✅ **built** — US-025…US-027: traces, admin read API, frozen eval set |
| **MOD-10 Client Surfaces** | ✅ **built** — US-007…US-009: web chat, browser voice, admin console |

**1194 tests passing** (`platform/.venv/Scripts/python -m pytest tests -q`), plus 54 browser
scenarios across desktop and mobile. The deterministic answer path is live over the real corpus:
price, MOQ, delivery, specifications, comparison and company facts, all read from SQLite with no
language model involved. One story is not finished — **US-001's real carrier call** — and the open
items are listed in [`../docs/sdlc/work-status.md`](../docs/sdlc/work-status.md) §9.

---

## Quick start

```powershell
cd D:\project\anitamedicalsystems\platform

py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in your values

# Validate the configuration. Starts nothing.
$env:PYTHONPATH="app"
$env:ANITA_ADMIN_TOKEN="<your-token>"
.\.venv\Scripts\python -m anita.boot --preflight
```

Exit codes: `0` bootable · `2` refused (config violates a rule) · `3` config could not be parsed.

### Tests

```powershell
.\.venv\Scripts\python -m pytest tests -q
```

---

## Why MOD-09 came first

It is not user-visible, and it is not the interesting part of the product. It is
first because **the failure it prevents is silent**.

If this instance pointed at the university deployment's database, vector
collection, port or log directory, everything would start, every health check
would pass, and the product would quietly serve the wrong answers and overwrite
the wrong leads. Nothing would crash. A crash is *better* than that bug — so the
guard turns it into a crash, deliberately, at boot.

The same reasoning applies to the config audit. A key that is written but never
read is worse than a missing key: a missing key fails loudly, while an inert key
looks configured, gets edited during an incident, and does nothing. The source
system had exactly this defect (`FASTAPI_WORKERS`, plus four inert RAG keys),
which is why the check exists here rather than as a later cleanup.

---

## What MOD-09 does

```
LOAD → AUDIT → ISOLATION → SECRETS → PLATFORM → DATA → WARM
```

| Phase | Refuses when |
|---|---|
| **LOAD** | A value is malformed — names the offending key, never falls back silently |
| **AUDIT** | A key is set for real but read by nothing (`INERT_KEY`) |
| **ISOLATION** | A database, collection, port or log path collides with the sibling product, or the VRAM budget would starve it |
| **SECRETS** | A required secret is unset — reports presence only, never a value |
| **PLATFORM** | The model backend cannot be selected |
| **DATA** | The corpus is missing. An answer engine with no catalogue can only invent |
| **WARM** | Never — a cold model is reported, not refused |

Every phase runs and reports, so an operator fixes all four collisions in one
pass rather than one per boot.

### Refusal output

```
BOOT REFUSED — isolation collision (BRD-18)
  resource : chroma_collection
  anita    : langchain
  sibling  : langchain
  action   : set ANITA_CHROMA_COLLECTION to a value unique to this product (e.g. anita_ams__v1)
```

A refused boot leaves **nothing** behind — no port bound, no connection opened,
no report written into the directory it just rejected.

### Isolation is logical, not physical

Per `CV-01`, the Twilio account and the GPU are **explicitly shared** with the
university deployment and are not checked. What is checked is that this instance
owns its own:

| Resource | ANITA | Sibling must not match |
|---|---|---|
| Database / schema | `anita_ams` / `anita` | `admissions` |
| Vector collection | `anita_ams__v1` | `langchain`, `meridian_kb__v1` |
| Ports | 8100 / 8601 / 8602 | 8000, 8501, 8502, 8098, 1234 |
| Log directory | `./logs/anita` | `logs`, `./logs` |

Forbidden values live in `config/anita.yaml` under `isolation_forbidden` — they
are configuration, not code, because the sibling's coordinates are the
operator's knowledge. They are seeded so the guard is useful out of the box.

---

## Layout

```
platform/
├── config/anita.yaml       # non-secret configuration + isolation coordinates
├── .env.example            # KEY NAMES ONLY — no values
├── app/anita/
│   ├── config.py           # frozen AnitaSettings + typed parsers
│   ├── config_truth.py     # read-coverage audit (stdlib-only)
│   ├── isolation.py        # the collision guard
│   ├── platform_detect.py  # CUDA → Metal → CPU; Ollama or MLX
│   ├── secrets.py          # masking policy
│   ├── boot.py             # preflight + CLI
│   └── errors.py
├── tests/
└── logs/anita/             # instance-scoped; never the shared logs/
```

`config_truth.py` is **stdlib-only on purpose**: it is the check that runs when
the stack will not start, so it cannot be part of the thing that failed to start.

---

## Notes

**Platform support is derived, never hand-set.** No configuration file differs
between Windows and macOS, and an operator never edits an OS-specific value to
make the stack start. On this machine the probe reports `cpu` because `torch` is
not installed — it says so explicitly rather than silently downgrading. Install
the CUDA build of torch to get `cuda`.

**Secrets are never rendered.** Any key matching
`TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL|SID|AUTH` is masked in every log
line, report and error message; only `set`/`unset` is ever printed. No value from
the source repository's `.env` files was copied — see `REC-21`.

**Two items block public launch** (not MOD-09's to fix): `DG-06` — auth, rate
limiting and Twilio webhook signature validation on the public surfaces; and
`DG-08` — legal review of the clinical-refusal wording.
