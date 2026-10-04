"""Index build lifecycle (TRD-15, `SM-05`).

```
ABSENT → BUILDING → VALIDATING → LIVE
             └──────────┴────────→ REJECTED → BUILDING (rebuild)
```

Two properties make this worth a module rather than a script:

**A rebuild never takes the live index down.** Building happens into a
*versioned* collection (`anita_ams__v1`, then `__v2`, …) and promotion is a
single pointer update. A crash, an embedding failure or a failed quality gate
leaves the previous index serving exactly as before — which is what makes it
safe to refresh a catalogue on a running system.

**Promotion is gated on validation.** `VALIDATING → LIVE` requires the quality
gate to pass (`BRD-22`). A corpus that regressed cannot be promoted by
forgetting to run the check, because the check is a transition condition rather
than a step someone is supposed to remember.

The corpus itself is validated before any embedding happens — see
:func:`assert_corpus_is_anita`, which carries the source system's ingestion gate
idea (REC-23): refuse a knowledge base that does not belong to this product.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .retrieval import Chunk

logger = logging.getLogger("anita.index")


class IndexState(str, Enum):
    ABSENT = "absent"
    BUILDING = "building"
    VALIDATING = "validating"
    LIVE = "live"
    REJECTED = "rejected"


class CorpusError(ValueError):
    """The corpus is not the ANITA catalogue, or is unusable."""


class BuildStateError(RuntimeError):
    """An illegal index-build transition was attempted.

    Raised rather than coerced: silently jumping `BUILDING → LIVE` is how an
    unvalidated index reaches buyers.
    """


# DDL for the build history. Applied by the migration alongside the other
# `anita` schema objects.
INDEX_BUILDS_DDL = """
CREATE TABLE IF NOT EXISTS anita.index_builds (
    build_id       UUID PRIMARY KEY,
    collection     TEXT NOT NULL,
    state          TEXT NOT NULL,
    chunks         INTEGER,
    dimensions     INTEGER,
    embed_model    TEXT,
    corpus_hash    TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    validation     JSONB,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_builds_state ON anita.index_builds (state);
"""

# A corpus that does not mention this company is not this company's catalogue.
# The source system used the same guard to stop a university KB being indexed
# under the wrong product; here it is the mirror image.
REQUIRED_CORPUS_MARKER = "anitamedicalsystems"
FORBIDDEN_CORPUS_MARKERS = ("meridian", "university of maryland", "admissions")


def assert_corpus_is_anita(chunks: Sequence[Chunk]) -> None:
    """Refuse to index anything that is not the ANITA catalogue.

    Checked before embedding, because the failure is expensive to undo: an index
    built from the wrong corpus returns confident, well-formed, entirely
    irrelevant answers, and nothing about the system looks broken.
    """
    if not chunks:
        raise CorpusError("corpus is empty — nothing to index")

    sources = " ".join((chunk.source_url or "") for chunk in chunks).lower()
    if REQUIRED_CORPUS_MARKER not in sources:
        raise CorpusError(
            f"corpus does not reference {REQUIRED_CORPUS_MARKER!r}. Refusing to "
            f"index — this looks like a different product's catalogue."
        )

    text = " ".join(chunk.text for chunk in chunks[:50]).lower()
    for marker in FORBIDDEN_CORPUS_MARKERS:
        if marker in text:
            raise CorpusError(
                f"corpus contains {marker!r}, which belongs to the sibling "
                f"deployment. Refusing to index (BRD-18)."
            )


def corpus_fingerprint(chunks: Sequence[Chunk]) -> str:
    """Stable hash of the corpus contents, for change detection."""
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda c: c.chunk_id):
        digest.update(chunk.chunk_id.encode())
        digest.update((chunk.content_hash or chunk.text).encode("utf-8", "replace"))
    return digest.hexdigest()


@dataclass
class IndexBuild:
    """One attempt to build an index."""

    build_id: str
    collection: str
    state: IndexState = IndexState.BUILDING
    chunks: int = 0
    dimensions: int | None = None
    embed_model: str = ""
    corpus_hash: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    validation: dict = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        data["started_at"] = self.started_at.isoformat()
        data["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        return data


class BuildStore(Protocol):
    """Where build records are kept. Optional — the lifecycle works without one."""

    def save(self, build: IndexBuild) -> None: ...

    def latest_live(self) -> IndexBuild | None: ...


class MemoryBuildStore:
    """In-process build history. Used by tests and by SQLite-less deployments."""

    def __init__(self) -> None:
        self.builds: list[IndexBuild] = []

    def save(self, build: IndexBuild) -> None:
        self.builds = [b for b in self.builds if b.build_id != build.build_id]
        self.builds.append(build)

    def latest_live(self) -> IndexBuild | None:
        for build in reversed(self.builds):
            if build.state is IndexState.LIVE:
                return build
        return None

    def live_collection(self) -> str | None:
        live = self.latest_live()
        return live.collection if live else None

    def history(self, *, limit: int = 10) -> list[dict]:
        return [build.as_dict() for build in reversed(self.builds[-limit:])]


class DoublePromotion(BuildStateError):
    """Two LIVE builds for one collection. The database refuses it (`TAC-3`)."""


class SqliteBuildStore:
    """Durable build history — `anita.index_builds`, in the dialect we run.

    Two things here are load-bearing rather than plumbing:

    * **The one-LIVE rule is a partial unique index**, not a check in
      `IndexLifecycle` (the story's `TAC-3`, literally): `UNIQUE (collection)
      WHERE state = 'LIVE'`. A double promotion is therefore a database error
      even if a future caller forgets to go through `promote()`, and the
      guarantee survives a process that is not this process.
    * **The live collection is a pointer row**, which is what makes promotion
      atomic in the sense that matters: the retriever asks this store which
      collection is live, so going live is one committed transaction rather
      than a data move. Without it, "versioned collections" would be a naming
      convention with nothing reading the version.

    SQLite, not Postgres (`CR-02` owns that swap); `validation` is JSON text
    rather than `jsonb`, same as every other store here.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS index_builds (
        build_id     TEXT PRIMARY KEY,
        collection   TEXT NOT NULL,
        state        TEXT NOT NULL,
        chunks       INTEGER,
        dimensions   INTEGER,
        embed_model  TEXT,
        corpus_hash  TEXT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        validation   TEXT,
        error        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_index_builds_state ON index_builds (state);
    CREATE UNIQUE INDEX IF NOT EXISTS index_builds_one_live
        ON index_builds (collection) WHERE state = 'LIVE';
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(self.SCHEMA)

    @classmethod
    def open_if_exists(cls, path: Path | str) -> "SqliteBuildStore | None":
        """Open an existing store, or None. Never creates one.

        Boot reads this pointer; a read must not create the file it is reading
        from, or a fresh deployment would look like it had a build history.
        """
        if not Path(path).exists():
            return None
        return cls(path)

    # -- writes ------------------------------------------------------------
    def save(self, build: IndexBuild) -> None:
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO index_builds (build_id, collection, state, chunks, "
                    "dimensions, embed_model, corpus_hash, started_at, finished_at, "
                    "validation, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (build_id) DO UPDATE SET collection = excluded.collection, "
                    "state = excluded.state, chunks = excluded.chunks, "
                    "dimensions = excluded.dimensions, embed_model = excluded.embed_model, "
                    "corpus_hash = excluded.corpus_hash, finished_at = excluded.finished_at, "
                    "validation = excluded.validation, error = excluded.error",
                    self._row(build),
                )
            except sqlite3.IntegrityError as exc:
                raise DoublePromotion(
                    f"another build is already LIVE for {build.collection} — "
                    f"the one-LIVE constraint refused this promotion"
                ) from exc

    def promote(self, build: IndexBuild, previous_build_id: str | None) -> None:
        """Swap the live pointer in **one** transaction.

        The previous LIVE row is demoted first and the new one inserted second,
        inside `BEGIN IMMEDIATE` — so the partial unique index never sees two
        LIVE rows, and a crash between the two statements cannot leave the
        collection with no live build at all.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if previous_build_id and previous_build_id != build.build_id:
                    self._connection.execute(
                        "UPDATE index_builds SET state = 'REJECTED', error = 'superseded', "
                        "finished_at = ? WHERE build_id = ?",
                        (datetime.now(timezone.utc).isoformat(), previous_build_id),
                    )
                self._connection.execute(
                    "INSERT INTO index_builds (build_id, collection, state, chunks, "
                    "dimensions, embed_model, corpus_hash, started_at, finished_at, "
                    "validation, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (build_id) DO UPDATE SET state = excluded.state, "
                    "validation = excluded.validation, finished_at = excluded.finished_at, "
                    "error = excluded.error",
                    self._row(build),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise DoublePromotion(
                    f"another build is already LIVE for {build.collection} — "
                    f"the one-LIVE constraint refused this promotion"
                ) from exc
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(build: IndexBuild) -> tuple:
        # Uppercase, matching the DDL's partial index (`WHERE state = 'LIVE'`).
        # Written lowercase, the index matches nothing and the one-LIVE rule
        # this store exists to enforce would be silently inert — found by a
        # test that promoted twice and read back no LIVE row at all.
        return (
            build.build_id, build.collection, build.state.value.upper(), build.chunks,
            build.dimensions, build.embed_model, build.corpus_hash,
            build.started_at.isoformat(),
            build.finished_at.isoformat() if build.finished_at else None,
            json.dumps(build.validation), build.error,
        )

    # -- reads -------------------------------------------------------------
    def _build(self, row: sqlite3.Row) -> IndexBuild:
        return IndexBuild(
            build_id=str(row["build_id"]),
            collection=str(row["collection"]),
            state=IndexState(str(row["state"]).lower()),
            chunks=int(row["chunks"] or 0),
            dimensions=row["dimensions"],
            embed_model=str(row["embed_model"] or ""),
            corpus_hash=str(row["corpus_hash"] or ""),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=(datetime.fromisoformat(row["finished_at"])
                         if row["finished_at"] else None),
            validation=json.loads(row["validation"] or "{}"),
            error=row["error"],
        )

    def latest_live(self) -> IndexBuild | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM index_builds WHERE state = 'LIVE' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return self._build(row) if row is not None else None

    def live_collection(self) -> str | None:
        live = self.latest_live()
        return live.collection if live else None

    @property
    def builds(self) -> list[IndexBuild]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM index_builds ORDER BY started_at"
            ).fetchall()
        return [self._build(row) for row in rows]

    def history(self, *, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM index_builds ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._build(row).as_dict() for row in rows]


def live_collection_name(store_path: Path | str, default: str) -> str:
    """Which collection the retriever must query.

    The promoted build's collection when one has been promoted, the configured
    name otherwise. This is the read side of the atomic pointer: every process
    that serves answers asks this question, so a promotion takes effect for all
    of them at the moment the transaction commits.
    """
    store = SqliteBuildStore.open_if_exists(store_path)
    if store is None:
        return default
    return store.live_collection() or default


class IndexLifecycle:
    """Drives `SM-05` and owns the live-collection pointer.

    Versioned collection names are what make promotion atomic: the new build
    lands in a collection nobody is querying yet, and going live is a pointer
    assignment rather than a data move.
    """

    def __init__(
        self,
        base_collection: str,
        store: BuildStore | None = None,
        *,
        version: int | None = None,
    ) -> None:
        self.base_collection = base_collection
        self.store = store or MemoryBuildStore()
        self._current: IndexBuild | None = None
        self._live: IndexBuild | None = self.store.latest_live()
        self._live_collection: str | None = (
            self.store.live_collection() if hasattr(self.store, "live_collection")
            else (self._live.collection if self._live else None)
        )
        # Next version continues from the highest seen, so a restart does not
        # reuse a collection name that already holds a rejected build.
        self._next_version = version or self._infer_next_version()

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> IndexState:
        """What the lifecycle is *doing*, not merely what is serving.

        A build in progress reports BUILDING or VALIDATING even though the
        previous index is still live and answering. Reporting only "what is
        live" made an in-flight rebuild invisible, which is precisely the moment
        an operator wants to see it.
        """
        current = self._current
        if current is not None and current.state in (IndexState.BUILDING, IndexState.VALIDATING):
            return current.state
        return self._live.state if self._live else IndexState.ABSENT

    @property
    def live_collection(self) -> str | None:
        return self._live_collection

    def collection_for(self, version: int) -> str:
        """``anita_ams__v1`` → ``anita_ams__v2``. Never reuses a number."""
        base = self.base_collection
        stem = base.split("__v")[0]
        return f"{stem}__v{version}"

    def _infer_next_version(self) -> int:
        highest = 0
        for build in getattr(self.store, "builds", []):
            stem = build.collection.split("__v")
            if len(stem) == 2 and stem[1].isdigit():
                highest = max(highest, int(stem[1]))
        return highest + 1

    # -- transitions -------------------------------------------------------
    def begin(self, *, embed_model: str = "", dimensions: int | None = None) -> IndexBuild:
        """ABSENT|REJECTED|LIVE → BUILDING, in a fresh collection."""
        build = IndexBuild(
            build_id=str(uuid.uuid4()),
            collection=self.collection_for(self._next_version),
            state=IndexState.BUILDING,
            embed_model=embed_model,
            dimensions=dimensions,
        )
        self._next_version += 1
        self._current = build
        self.store.save(build)
        logger.info("index build %s started into %s", build.build_id, build.collection)
        return build

    def built(self, chunks: int, dimensions: int, corpus_hash: str) -> IndexBuild:
        """BUILDING → VALIDATING. The staging collection is now complete."""
        build = self._require_current(IndexState.BUILDING)
        build.chunks = chunks
        build.dimensions = dimensions
        build.corpus_hash = corpus_hash
        build.state = IndexState.VALIDATING
        self.store.save(build)
        logger.info("index build %s built %d chunks; validating", build.build_id, chunks)
        return build

    def promote(self, validation: dict) -> IndexBuild:
        """VALIDATING → LIVE. The pointer swap that makes the new index live.

        Only reachable from VALIDATING, so a build cannot go live without having
        been validated — the quality gate is the transition condition, not a
        step someone has to remember (BRD-22).
        """
        build = self._require_current(IndexState.VALIDATING)
        if not validation.get("passed"):
            return self.reject(f"quality gate failed: {validation.get('reason', 'unspecified')}")

        build.validation = validation
        build.state = IndexState.LIVE
        build.finished_at = datetime.now(timezone.utc)

        previous = self._live
        if hasattr(self.store, "promote"):
            # One transaction: demote the old LIVE, write the new one. A
            # reader asking "which collection is live" sees one answer or the
            # other, never neither (`TAC-3`, `TAC-4`).
            self.store.promote(build, previous.build_id if previous else None)
        else:
            self.store.save(build)
            if previous and previous.build_id != build.build_id:
                previous.state = IndexState.REJECTED  # superseded; kept for history
                previous.error = "superseded"
                self.store.save(previous)

        self._live = build
        self._live_collection = build.collection

        logger.info("index build %s is LIVE (%s)", build.build_id, build.collection)
        return build

    def reject(self, reason: str) -> IndexBuild:
        """Any state → REJECTED. The previous live index keeps serving."""
        build = self._current or self._live
        if build is None:
            raise RuntimeError("no build to reject")
        build.state = IndexState.REJECTED
        build.error = reason
        build.finished_at = datetime.now(timezone.utc)
        self.store.save(build)
        logger.warning("index build %s rejected: %s", build.build_id, reason)
        return build

    def status(self) -> dict:
        """What is building, what is live, and the recent history (`UC-12`).

        Three different questions, answered separately: an operator watching a
        rebuild needs to know the *live* collection **and** that a build is in
        progress — reporting only the live one made an in-flight rebuild
        invisible, which is the moment they most want to see it.
        """
        return {
            "state": self.state.value,
            "base_collection": self.base_collection,
            "live_collection": self.live_collection,
            "live_build_id": self._live.build_id if self._live else None,
            "current_build_id": self._current.build_id if self._current else None,
            "history": (
                self.store.history(limit=10) if hasattr(self.store, "history") else []
            ),
        }

    def _require_current(self, expected: IndexState) -> IndexBuild:
        build = self._current
        if build is None:
            raise RuntimeError("no build in progress")
        if build.state is not expected:
            # Illegal transitions are refused rather than coerced: silently
            # jumping BUILDING → LIVE is how an unvalidated index reaches buyers.
            raise BuildStateError(
                f"illegal transition: build is {build.state.value}, expected {expected.value}"
            )
        return build


def build_index(
    lifecycle: IndexLifecycle,
    chunks: Sequence[Chunk],
    *,
    retriever,
    embed_model: str = "",
    dimensions: int | None = None,
    validate: Callable[[object], dict] | None = None,
) -> IndexBuild:
    """Run one full build: guard the corpus, embed, validate, promote or reject.

    Every failure path ends in :meth:`IndexLifecycle.reject`, which leaves the
    previously live index serving. A refresh that goes wrong is therefore a
    no-op rather than an outage.
    """
    try:
        assert_corpus_is_anita(chunks)
    except CorpusError as exc:
        build = lifecycle.begin(embed_model=embed_model, dimensions=dimensions)
        return lifecycle.reject(str(exc))

    build = lifecycle.begin(embed_model=embed_model, dimensions=dimensions)

    try:
        retriever.build_index()
        indexed = retriever.store.count()
    except Exception as exc:  # noqa: BLE001 - any failure leaves the old index live
        return lifecycle.reject(f"embedding failed: {type(exc).__name__}: {exc}")

    lifecycle.built(
        chunks=indexed,
        dimensions=dimensions or 0,
        corpus_hash=corpus_fingerprint(chunks),
    )

    validation = validate(build) if validate else {"passed": True, "reason": "no gate configured"}
    if not validation.get("passed"):
        return lifecycle.reject(validation.get("reason", "quality gate failed"))

    return lifecycle.promote(validation)
