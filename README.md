# anitamedicalsystems — data collection pipeline

Collects **every page of https://www.anitamedicalsystems.com** and emits the
corpus in formats consumable by vector, graph, tabular and Q&A RAG systems.

This repository performs **data collection only**. No retrieval, embedding or
chat logic lives here — that is the next phase.

---

## Quick start

```powershell
cd D:\project\anitamedicalsystems

py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

.\.venv\Scripts\python scripts\run_all.py
```

Then open `data/out/reports/validation.md`.

To work offline from an existing cache:

```powershell
.\.venv\Scripts\python scripts\run_all.py --skip-crawl
```

---

## What the site is

A TradeIndia-hosted seller microsite for **ANITA MEDICAL SYSTEMS PVT. LTD.**
(medical imaging equipment distributor, Mumbai + New Delhi).

| | |
|---|---|
| Pages | **32** — 24 products, 3 categories, plus home, catalog, company profile, contact, HTML sitemap |
| Rendering | Fully server-rendered. No browser needed |
| Page weight | 380–620 KB each, of which **~98% is inline CSS, JS templates and inquiry modals** |
| Real content | ~50–70K tokens in total — small, but dense with structured facts |

---

## Outputs

Everything lands in `data/out/`.

| File | RAG type | Contents |
|---|---|---|
| `chunks.jsonl` | Vector / naive | Section-per-chunk with a self-containment prefix |
| `qa_pairs.jsonl` | Q&A | 36 verbatim site FAQs + derived fact/contact answers |
| `voice_short_answers.jsonl` | Voice + WhatsApp | The same Q&A with speakable answers (≤ ~40 words) |
| `gaps.jsonl` | All | Questions the site **cannot** answer, so the bot can hand off honestly |
| `graph_nodes.jsonl` / `graph_edges.jsonl` | Graph | 120 nodes / 283 edges, evidence-backed only |
| `tables/*.csv` + `ams.sqlite` | Tabular / text-to-SQL | 10 normalised tables with numeric columns and indexes |
| `media_manifest.json` | Multimodal | Image URLs, roles and size variants (**manifest only, no downloads**) |
| `api_inventory.json` + `API_INVENTORY.md` | Integration | Every form/AJAX endpoint, with robots status |
| `site_shared.json` | All | Nav, addresses, GST, phone — stored **once**, not 32 times |
| `url_manifest.json` | — | Every discovered URL and its page type |
| `reports/validation.md` | — | The 10 validation gates |

### Consuming each output

**Vector RAG** — embed `embed_text` (context prefix + text), display `text`.
Chunks are already self-contained; do **not** re-split them.

**Tabular / text-to-SQL** — most high-value questions here are *lookups*, not
semantic search. Answer them from SQL, where they cannot hallucinate:

```sql
SELECT name, price_amount, moq_value, delivery_days
FROM products WHERE price_amount < 100000 ORDER BY price_amount;
```

**Voice / WhatsApp** — use `speakable_answer`. Money is rendered in Indian
convention ("about 14.5 lakh rupees"), because a TTS engine reading `1450000`
is unintelligible.

**Graph RAG** — traverse `graph_edges.jsonl` from `company:anita-medical-systems`.

---

## Pipeline

```
01_discover  ->  02_fetch  ->  03_parse  ->  04_emit  ->  05_validate
  union of        cache-first     container-    6 emitters   10 gates,
  4 sources       + polite        anchored      from one     non-zero exit
                  + backoff       extraction     canonical     on failure
                                                record set
```

Every emitter reads the same canonical `PageDocument` set in `data/pages/`.
Nothing re-parses HTML, and no emitter depends on another — so the outputs
cannot disagree with each other, and fixing one cannot break another.

---

## Site-specific hazards (all verified, all handled)

These are the traps this pipeline exists to avoid. Each was reproduced live.

1. **Draft.js word-splitting.** The prose markup puts *every word in its own
   `<span>`*: `' X'` / `'-'` / `'Ray'`. Stripping tags with a space separator
   corrupts it into `"X - Ray"`, `"film ."`, `"cass ettes"`. Inline elements
   must join with **no** separator; block elements with a space. See
   `dom.smart_text` — the single most important function here.

2. **The corruption is also baked into the JSON-LD.** `Product.description`
   literally reads `"An X - Ray cassette ... on a film ."` and cannot be
   reversed. **Never take prose from JSON-LD** — the DOM `About` text is clean
   and is used instead.

3. **HTTP 403 rate-limiting.** The host blocks sustained crawling. Every
   response is cached before anything else, requests are sequential with a 2 s
   delay, failures back off exponentially, and a circuit breaker aborts the run
   rather than hammering. A block therefore never costs already-fetched pages.

4. **Boilerplate dominance.** Nav, footer, addresses and inquiry modals repeat
   on all 32 pages. They are stripped structurally and the site-wide facts are
   stored once in `site_shared.json`.

5. **Absence is data.** 3 products have no specification block and 4 have an
   empty About section **on the source site**. These are recorded as
   `section_absent_on_source:*` rather than reported as extraction failures, so
   the validation gate stays meaningful.

6. **`<h3>` is overloaded.** It marks both FAQ questions and related-product
   names. Counting headings over-reports FAQs by ~4 per page; only an `<h3>`
   whose text begins `Q:` is a real FAQ.

7. **Duplicate product names.** Two products are both named "Medical Dry
   Imager" at different prices. Chunk context prefixes include the numeric
   product id so a voice answer cannot conflate them.

8. **Mis-authored robots.txt.** The `Disallow` block follows
   `User-agent: Mediapartners-Google` with no intervening `User-agent` line, so
   strictly it binds only that crawler. We treat it as binding anyway — it names
   the inquiry, OTP, callback and login machinery, and respecting the site's
   clear intent is the conservative choice.

9. **`Disallow: /?fd=1`** has `/` as its path component. Naive prefix matching
   flags *every* endpoint as disallowed. Handled explicitly in
   `api_inventory._is_disallowed`.

10. **Windows cp1252.** Consoles crash on characters like `✕` (U+2715). Every
    entry point calls `config.force_utf8()`.

---

## Robots & etiquette

- `robots.txt` is fetched and honoured; disallowed paths are never requested.
- The crawler is **sequential** with a 2 s delay and exponential backoff.
- API endpoints are documented by **static analysis only** — none is ever
  called. 24 of the 29 endpoints sit under Disallow paths.
- No image binaries are downloaded (manifest only).

---

## Notes for the chat phase

- **Prefer SQL over embeddings for facts.** Price, MOQ, delivery, GST, address
  and director names are exact lookups; embeddings would add hallucination risk
  for no benefit. Reserve embeddings for About prose and FAQs.
- **Always quote prices with the caveat.** `products.price_is_list_price = 1`
  and `quote_required = 1` — the site shows list prices and requires a
  quotation. A bot quoting ₹14,50,000 as final is a commercial liability.
- **Use `gaps.jsonl`** so unmatched questions produce an honest handoff to the
  phone number rather than an invention.
- **Tag answers `en-IN`** and expect Hinglish/transliterated queries.
- **Traceability:** every row carries `source_url`; medical-equipment answers
  should cite it.

---

## Layout

```
├── config/site.yaml          # all site-specific settings, rules and selectors
├── src/ams/
│   ├── config.py  models.py  pipeline.py  validate.py
│   ├── fetch.py              # cache-first, polite, block-aware HTTP
│   ├── discover.py           # 4-source URL union
│   ├── dom.py                # smart_text — the inline/block text walker
│   ├── normalize.py          # money, quantities, Indian numerals, cleanup
│   ├── parse/                # one module per page type
│   └── emit/                 # one module per output family
├── scripts/                  # 01_discover … 05_validate, run_all
├── tests/                    # 48 unit tests
└── data/{raw,pages,out}/     # cache, canonical records, outputs
```

## Tests

```powershell
.\.venv\Scripts\python -m pytest tests -q
```
