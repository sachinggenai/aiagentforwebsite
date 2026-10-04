# CR-02 — Production infrastructure: containerisation and a server database

> **Lens:** PO (scope) · **Decided by:** user, 2026-10-03 · **Inputs:** CR-01 contract, `platform/requirements.txt`, `platform/app/anita/config.py`
> **Status:** IN ANALYSIS — decisions in §3 need confirming before build

## 1. The request (user-provided)

> "make sure you build a production grade solution, as need services can be deployed in docker
> desktop if need, instead of sqlite use some better db mysql or something better open source"

Three requirements:

| # | Requirement |
|---|---|
| CR-02.1 | Production-grade: the services must be deployable, not just runnable on a laptop |
| CR-02.2 | **Docker Desktop deployable** — containers for the services |
| CR-02.3 | **Replace SQLite** with a better open-source server database |

## 2. Verified current state

| Store | Where | Contents | Evidence |
|---|---|---|---|
| `data/out/ams.sqlite` | corpus | 10 tables — products, specs, faqs, media, people, branches, certifications, company_ids, categories, related_products | `data/out/` |
| `logs/anita_crm.sqlite` | CRM | leads, handoffs, outbox — built by `MockCrmStore` | `app/anita/crm.py:256-270` |
| Chroma | not installed | index is `InMemoryStore`, rebuilt per process (~25s) | CR-01 Appendix B |

**Both are SQLite files. Neither is a server.** They do not survive a container restart, cannot be
shared between replicas, and have no concurrent-writer story.

## 3. Decisions needed

### D-1 — Which database? **Recommendation: PostgreSQL, not MySQL**

| | PostgreSQL | MySQL |
|---|---|---|
| Already declared? | **Yes** — `psycopg2-binary` is in `platform/requirements.txt:20` | No driver declared |
| Already used by the sibling? | **Yes** — universityDemo's sentiment `models.py` is Postgres | No |
| Shared-core fit | CR-01's `SentimentStore` Protocol maps onto the existing Postgres schema | Would need a new schema |

**PostgreSQL is the honest recommendation** on evidence, not preference: the driver is already a
declared dependency, the sibling product already runs it, and CR-01's store Protocol was designed
against a Postgres implementation. MySQL would add a second database engine to the estate for no
gain.

### D-2 — One database or two?

| Option | Trade-off |
|---|---|
| **Two logical databases, one server** (recommended) | Corpus is read-only and rebuilt by the scraper; CRM is read-write and transactional. Different lifecycles, different backup policies. Same server, separate DBs, separate roles |
| One database, separate schemas | Simpler operationally; couples scraper rebuilds to live CRM data |
| Two servers | Strongest isolation; heaviest operationally |

### D-3 — What the corpus becomes

The corpus is currently **generated** by the scraper into SQLite. Options: (a) the scraper writes
directly to Postgres; (b) the scraper keeps emitting SQLite/JSONL and a loader step imports it.

**Recommendation: (b).** The scraper is a batch job with its own reproducibility guarantees
(validation gates, determinism check). Coupling it to a live server database means a rebuild needs
database credentials and can half-fail against production. A load step keeps the corpus artefact
immutable and the import idempotent.

### D-4 — Docker topology

Proposed `compose.yaml` services:

| Service | Image | Notes |
|---|---|---|
| `api` | built from `platform/` | FastAPI on 8100 |
| `chat` | same image, different command | Streamlit on 8085 |
| `db` | `postgres:16-alpine` | 5432 — **already in the isolation guard's forbidden list** |
| `ollama` | `ollama/ollama` | 11434 — **shared with the sibling; do not containerise twice** |

**Note the collision:** `5432` is in `ForbiddenValues.port` (the sibling's Postgres). Containerised
ANITA needs a *different host port* — the guard is already correct, and this is the first time it
bites.

## 4. Impact

| Area | Impact |
|---|---|
| `isolation.py` | `db_name` check already guards `admissions`; add the new DSN to the audit |
| `.env` | `DATABASE_URL`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` — **I removed these earlier as unused placeholders.** They return, now genuinely read |
| `MockCrmStore` | becomes `PostgresCrmStore` behind the existing `CrmStore` Protocol (`crm.py:179-201`) |
| CR-01 | `SentimentStore` Protocol's Postgres adapter moves from "deferred" to in-scope (US-115 deferred it per CR-01 §C7) |
| Scraper | unchanged if D-3 is (b) |
| Tests | need a disposable Postgres — `pytest` against a container, or an in-memory double |

## 5. Open questions

- **Q9** Confirm PostgreSQL over MySQL (D-1)
- **Q10** Confirm two logical databases on one server (D-2)
- **Q11** Confirm the load-step approach for the corpus (D-3)
- **Q12** Should the corpus loader live in the scraper repo or the platform?

## 6. The honest constraint

**This is a substantial piece of work and should not be rushed.** CR-01 alone is 10 stories, and
CR-02 changes the persistence layer underneath them. Building both in one pass — at the end of a
long session, without the same evidence discipline — is how the defects this session found nine
times got written in the first place.

**Recommended sequence:** confirm D-1…D-3, then build CR-01 (US-101…US-105, US-111…US-115) against
SQLite as it stands — the stories are written for the current stores — then CR-02 migrates the
persistence layer underneath a working, tested system. Migrating the database and the module
architecture simultaneously makes any regression ambiguous.
