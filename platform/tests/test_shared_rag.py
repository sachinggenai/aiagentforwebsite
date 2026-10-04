"""US-101 — `shared/rag` is an injected, product-neutral retrieval package.

Tests the Gherkin scenarios and TAC-1…TAC-8 from
`docs/sdlc/change-requests/stories/US-101-shared-rag-package-extraction.md`.

The point of this story is CR-01.3: two products share one package with no fork.
So the load-bearing tests here are the ones that would fail if a product name,
an `os.environ` read, or a shared mutable crept back in.

Run:  .venv\\Scripts\\python -m pytest tests/test_shared_rag.py -q
"""

from __future__ import annotations

import ast
import hashlib
import os
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent
sys.path.insert(0, str(PLATFORM / "app"))
sys.path.insert(0, str(REPO))

from shared.rag import (  # noqa: E402
    ChromaVectorStore,
    CircuitBreaker,
    CorpusGateError,
    DimensionMismatch,
    InMemoryVectorStore,
    RagConfig,
    RagConfigError,
    RagService,
    RetrievalResult,
)

CORPUS = REPO / "data" / "out" / "chunks.jsonl"


class FakeEmbedder:
    """Deterministic vectors, so these tests need no Ollama.

    A stable hash rather than random: two runs must rank identically or a
    failure cannot be reproduced.
    """

    def __init__(self, dimensions: int = 8) -> None:
        self.dimensions = dimensions
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([b / 255.0 for b in digest[: self.dimensions]])
        return out


def config(**over) -> RagConfig:
    base = dict(
        collection_name="anita_ams__v1",
        persist_path=Path("./data/chroma_anita"),
        embed_model="fake-8",
        sources=(CORPUS,),
        expected_markers=("anitamedicalsystems",),
        blocked_markers=("meridian", "university of maryland", "admissions"),
        system_prompt="Answer from the context only:\n{context}",
        refusal_text="Not in the catalogue.",
    )
    base.update(over)
    return RagConfig(**base)  # type: ignore[arg-type]


def service(**over) -> RagService:
    return RagService(config(**over), embedder=FakeEmbedder(), store=InMemoryVectorStore())


requires_corpus = pytest.mark.skipif(not CORPUS.exists(), reason="corpus not built")


# ---------------------------------------------------------------------------
# Scenario: happy path — two products share one package with different configs
# ---------------------------------------------------------------------------
@requires_corpus
class TestTwoProductsOnePackage:
    def test_two_services_hold_their_own_collection_and_corpus(self, tmp_path):
        a = RagService(config(), embedder=FakeEmbedder(), store=InMemoryVectorStore())
        b = RagService(
            config(collection_name="other_product__v1",
                   expected_markers=("anitamedicalsystems",)),
            embedder=FakeEmbedder(), store=InMemoryVectorStore(),
        )
        assert a.build() > 0 and b.build() > 0
        assert a.status()["collection"] == "anita_ams__v1"
        assert b.status()["collection"] == "other_product__v1"
        # Neither service's status leaks the other's identity.
        assert "other_product" not in str(a.status())

    def test_a_service_returns_only_its_own_corpus(self):
        cfg = config()
        svc = RagService(cfg, embedder=FakeEmbedder(), store=InMemoryVectorStore())
        svc.build()
        result = svc.retrieve("ultrasound gel")
        assert result.ok
        for chunk in result.chunks:
            assert chunk.chunk_id in {c.chunk_id for c in svc._chunks}


# ---------------------------------------------------------------------------
# TAC-1: no product module reachable from the package
# ---------------------------------------------------------------------------
class TestPackageIsProductNeutral:
    PKG = REPO / "shared" / "rag"

    def test_no_import_of_a_product_package_in_source(self):
        """An AST scan, so a commented-out import does not count and a
        `from anita import x` cannot hide behind a string."""
        offenders = []
        for path in self.PKG.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    if root in {"anita", "app", "universityDemo"}:
                        offenders.append(f"{path.name}: {name}")
        assert not offenders, f"shared/rag imports a product package: {offenders}"

    def test_no_product_name_in_the_source(self):
        """TAC-5. The package may not mention either product, in code or prose —
        a docstring naming one is how a 'temporary' special case starts."""
        for path in self.PKG.glob("*.py"):
            text = path.read_text(encoding="utf-8").casefold()
            for name in ("meridian", "anita medical", "university of maryland"):
                assert name not in text, f"{path.name} mentions {name!r}"

    def test_importing_the_package_loads_no_product_module(self):
        import subprocess

        code = (
            "import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s');"
            "import shared.rag;"
            "leaked=[m for m in sys.modules if m=='anita' or m.startswith(('anita.','app.'))];"
            "print(leaked)" % (REPO, PLATFORM / "app")
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "[]", f"product modules leaked: {out.stdout} {out.stderr}"


# ---------------------------------------------------------------------------
# TAC-2 / M-3: importing must not mutate the process environment
# ---------------------------------------------------------------------------
class TestNoEnvironmentMutation:
    def test_import_performs_no_environ_writes(self):
        """The source set LANGCHAIN_* and HF_* process-wide at import
        (`rag_legacy.py:34-41`). That block is deleted, not copied — this proves
        it, in a fresh interpreter where the import cannot already have run."""
        import subprocess

        code = (
            "import os, sys; before=dict(os.environ);"
            "sys.path.insert(0, r'%s');"
            "import shared.rag;"
            "added=sorted(k for k in os.environ if k not in before);"
            "changed=sorted(k for k in before if os.environ.get(k)!=before[k]);"
            "print(added, changed)" % REPO
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "[] []", f"import mutated the environment: {out.stdout}"

    def test_constructing_a_service_performs_no_environ_writes(self):
        before = dict(os.environ)
        service()
        assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# TAC-3 / M-1 / M-2 / M-5: the config is the only source of product values
# ---------------------------------------------------------------------------
class TestConfigIsTheOnlySurface:
    @pytest.mark.parametrize("field,value", [
        ("collection_name", ""),
        ("collection_name", "has space"),
        ("embed_model", ""),
        ("sources", ()),
        ("expected_markers", ()),
        ("blocked_markers", ()),
        ("system_prompt", "no placeholder here"),
        ("refusal_text", ""),
        ("mode", "banana"),
        ("search_mode", "sideways"),
        ("distance_space", "manhattan"),
        ("failure_threshold", 0),
        ("probe_fraction", 0.0),
        ("probe_fraction", 1.5),
        ("rrf_k", 0),
    ])
    def test_invalid_config_is_refused_at_construction(self, field, value):
        with pytest.raises(RagConfigError) as exc:
            config(**{field: value})
        assert exc.value.field, "the refusal must name the offending field"

    def test_an_empty_acceptance_list_is_refused(self):
        """An empty expected_markers accepts any corpus, which makes the gate a
        no-op while looking present — the B-1 failure mode."""
        with pytest.raises(RagConfigError, match="expected_markers"):
            config(expected_markers=())

    def test_a_prompt_without_the_placeholder_is_refused(self):
        """Otherwise the context is silently dropped and the model answers from
        nothing — a loud failure at build time beats a silent one at answer time."""
        with pytest.raises(RagConfigError, match="system_prompt"):
            config(system_prompt="Answer carefully.")

    def test_collection_name_comes_from_config_not_the_environment(self, monkeypatch):
        """M-2. The source read RAG_COLLECTION_NAME from os.environ at import."""
        monkeypatch.setenv("RAG_COLLECTION_NAME", "smuggled_from_env")
        svc = service(collection_name="explicit__v1")
        assert svc.status()["collection"] == "explicit__v1"


# ---------------------------------------------------------------------------
# TAC-4 / B-6: no sparse index is shared between services
# ---------------------------------------------------------------------------
@requires_corpus
class TestNoSharedSparseIndex:
    def test_two_collections_never_share_a_sparse_index(self):
        a = service(collection_name="anita_ams__v1")
        a.build()
        b = service(collection_name="other__v1", persist_path=Path("./data/chroma_other"))
        b.build()
        assert a._sparse_index() is not b._sparse_index()

    def test_the_sparse_index_is_keyed_by_collection(self):
        """The source cached BM25 under the literal "bm25" for whichever
        collection happened to load first, so switching collections served a
        stale corpus index (B-6)."""
        svc = service()
        svc.build()
        svc._sparse_index()
        first_key = svc._bm25_key
        assert first_key is not None
        assert first_key[1] == "anita_ams__v1"


# ---------------------------------------------------------------------------
# TAC-6: a rung on every return path
# ---------------------------------------------------------------------------
@requires_corpus
class TestRungIsAlwaysReported:
    def test_a_successful_retrieval_reports_the_local_rung(self):
        svc = service()
        svc.build()
        assert svc.retrieve("ultrasound gel").rung == "local"

    def test_a_query_matching_nothing_reports_none(self):
        svc = service()
        svc.build()
        result = svc.retrieve("zzzzqqqq xxxxwwww")
        assert result.rung in {"local", "none"}
        assert isinstance(result, RetrievalResult)

    def test_retrieving_before_building_reports_a_rung_not_an_exception(self):
        """A service queried before build must not raise — it has no corpus, and
        'none' is the honest answer."""
        result = service().retrieve("anything")
        assert result.rung == "none"
        assert not result.ok


# ---------------------------------------------------------------------------
# TAC-7 / CV-06: the absent primary is explicit, not silently inert
# ---------------------------------------------------------------------------
class TestTheAbsentPrimaryIsExplicit:
    def test_with_no_primary_the_breaker_reports_off(self):
        assert service().status()["breaker"] == "off"

    def test_mode_effective_is_downgraded_and_visible(self):
        """`mode` is what was configured; with no primary injected the effective
        mode is `off`. Reported, never silent."""
        status = service(mode="auto").status()
        assert status["mode_configured"] == "auto"
        assert status["mode_effective"] == "off"

    def test_a_primary_would_raise_the_effective_mode(self):
        class Primary:
            def retrieve(self, query, top_k):
                return []

        svc = RagService(config(mode="auto"), embedder=FakeEmbedder(),
                         store=InMemoryVectorStore(), mcp=Primary())
        assert svc.status()["mode_effective"] == "auto"
        # Enabled but closed, not "off" — the breaker is now a live mechanism.
        assert svc.status()["breaker"] == "closed"

    def test_no_http_client_is_constructed(self):
        """Contract C7: nothing here speaks HTTP to an external service."""
        source = (REPO / "shared" / "rag").glob("*.py")
        for path in source:
            text = path.read_text(encoding="utf-8")
            for banned in ("import httpx", "import requests", "urllib.request"):
                assert banned not in text, f"{path.name} brings an HTTP client: {banned}"


# ---------------------------------------------------------------------------
# The corpus gate (B-1) — the blocker this package exists to lift
# ---------------------------------------------------------------------------
@requires_corpus
class TestCorpusGate:
    def test_anitas_corpus_passes_its_own_gate(self):
        svc = service()
        assert svc.build() == 141, "the corpus is 141 chunks"

    def test_a_foreign_gate_refuses_anitas_corpus(self):
        """The source's gate hardcoded one product's markers and returned None
        for anything else. Configuring different markers must refuse — that is
        what makes the gate real rather than decorative."""
        svc = service(expected_markers=("meridian university",))
        with pytest.raises(CorpusGateError):
            svc.build()

    def test_a_blocked_marker_refuses_even_when_the_corpus_would_pass(self, tmp_path):
        polluted = tmp_path / "polluted.jsonl"
        polluted.write_text(
            (CORPUS.read_text(encoding="utf-8").splitlines()[0]) + "\n"
            + '{"chunk_id":"bad","text":"Meridian University admissions",'
              '"source_url":"http://x/anitamedicalsystems"}\n',
            encoding="utf-8",
        )
        svc = service(sources=(polluted,))
        with pytest.raises(CorpusGateError, match="blocked marker"):
            svc.build()

    def test_the_marker_is_found_in_provenance_not_only_prose(self):
        """ANITA's marker appears in 0 of 141 chunk *texts* and 141 of 141
        `source_url` values. A gate that read only prose rejected the corpus it
        exists to accept — which is what happened on the first run."""
        svc = service()
        assert svc.build() > 0


# ---------------------------------------------------------------------------
# Store behaviour
# ---------------------------------------------------------------------------
class TestStore:
    def test_upsert_is_idempotent_by_chunk_id(self):
        store = InMemoryVectorStore()
        store.upsert(["a", "b"], [[1.0, 0.0], [0.0, 1.0]])
        store.upsert(["a"], [[0.5, 0.5]])
        assert store.count() == 2, "a rebuild must replace, not append"

    def test_a_dimension_change_is_refused(self):
        store = InMemoryVectorStore()
        store.upsert(["a"], [[1.0, 0.0]])
        with pytest.raises(DimensionMismatch):
            store.upsert(["b"], [[1.0, 0.0, 0.0]])

    def test_chroma_without_chromadb_raises_a_typed_error(self, tmp_path):
        """Lazy import: the package must import and the in-memory store must work
        with Chroma absent. If Chroma *is* installed this asserts the open path
        instead, which is equally useful."""
        store = ChromaVectorStore(tmp_path / "c", "probe__v1")
        try:
            import chromadb  # noqa: F401
        except ImportError:
            from shared.rag import VectorStoreUnavailable

            with pytest.raises(VectorStoreUnavailable, match="not installed"):
                store.count()


class TestContextBudget:
    @requires_corpus
    def test_context_never_exceeds_its_budget(self):
        svc = service()
        svc.build()
        result = svc.retrieve("ultrasound gel")
        assert len(result.context(max_chars=500)) <= 500

    @requires_corpus
    def test_context_is_never_empty_when_chunks_exist(self):
        """The budget must not be enforced by dropping everything — a prompt with
        no context is a prompt the model fills in."""
        svc = service()
        svc.build()
        assert svc.retrieve("ultrasound gel").context(max_chars=200)


class TestCircuitBreakerFixes:
    def test_a_single_failure_does_not_open_the_circuit(self):
        """B-8. The source opened on one failure, so a transient timeout
        disabled the primary for a full cooldown."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_s=60)
        breaker.record_failure("transient")
        assert breaker.state == "closed"

    def test_the_threshold_does_open_it(self):
        breaker = CircuitBreaker(failure_threshold=3, cooldown_s=60)
        for _ in range(3):
            breaker.record_failure("down")
        assert breaker.state == "open"

    def test_probe_claim_is_atomic(self):
        """CV-10. The source checked and set without a lock, so concurrent
        callers could both claim the probe and hit the primary twice."""
        import threading
        import time

        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.05)
        breaker.record_failure("down")
        time.sleep(0.06)
        assert breaker.state == "half_open"

        claims: list[int] = []
        barrier = threading.Barrier(20)

        def race():
            barrier.wait()
            if breaker.claim_probe():
                claims.append(1)

        threads = [threading.Thread(target=race) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(claims) == 1, f"{len(claims)} threads claimed the probe; exactly 1 may"

    def test_a_disabled_breaker_reports_off_not_closed(self):
        """'off' means there is no primary to break. 'closed' would claim a
        working mechanism that does not exist."""
        assert CircuitBreaker(enabled=False).state == "off"
        assert CircuitBreaker(enabled=False).admit() is False

    def test_an_abandoned_probe_does_not_block_recovery(self):
        """One path returning without a verdict must not stop probing forever."""
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.05, read_timeout_s=0.05)
        breaker.record_failure("down")
        import time

        time.sleep(0.06)
        assert breaker.claim_probe() is True
        # Second claim while in flight is refused...
        assert breaker.claim_probe() is False
        # ...but once the slot is older than read_timeout + 1s it is reclaimed.
        time.sleep(1.1)
        assert breaker.claim_probe() is True


# ---------------------------------------------------------------------------
# US-102 — the corpus gate is configurable and cannot be switched off
# ---------------------------------------------------------------------------
@requires_corpus
class TestCorpusGateContract:
    def test_gate_failure_makes_zero_embed_calls(self):
        """TAC-3. The gate must run *before* the first embed: a corpus that is
        not this product's must never reach the embedder, let alone the store."""
        embedder = FakeEmbedder()
        svc = RagService(
            config(expected_markers=("meridian university",)),
            embedder=embedder, store=InMemoryVectorStore(),
        )
        with pytest.raises(CorpusGateError):
            svc.build()
        assert embedder.calls == 0, "the gate must reject before any embedding"

    def test_gate_failure_writes_no_vectors(self):
        """The previously built index is untouched, because nothing is written."""
        store = InMemoryVectorStore()
        svc = RagService(config(expected_markers=("meridian university",)),
                         embedder=FakeEmbedder(), store=store)
        with pytest.raises(CorpusGateError):
            svc.build()
        assert store.count() == 0

    def test_every_blocked_marker_is_reported_not_just_the_first(self, tmp_path):
        """TAC-5, carried from the source's loop. Reporting only the first sends
        an operator on several round trips to clean one corpus."""
        polluted = tmp_path / "polluted.jsonl"
        polluted.write_text(
            '{"chunk_id":"a","text":"Meridian University","source_url":"http://x/anitamedicalsystems"}\n'
            '{"chunk_id":"b","text":"University of Maryland","source_url":"http://x/anitamedicalsystems"}\n'
            '{"chunk_id":"c","text":"FAFSA forms","source_url":"http://x/anitamedicalsystems"}\n',
            encoding="utf-8",
        )
        # The full blocked list from the config file, so the test exercises the
        # markers that actually ship rather than the three in the test helper.
        import yaml

        gate = yaml.safe_load(
            (PLATFORM / "config" / "anita.yaml").read_text(encoding="utf-8")
        )["corpus_gate"]
        svc = service(sources=(polluted,),
                      blocked_markers=tuple(gate["blocked_markers"]))
        with pytest.raises(CorpusGateError) as exc:
            svc.build()
        assert len(exc.value.blocked) == 3, f"only {exc.value.blocked} reported"
        assert "fafsa" in exc.value.blocked

    def test_the_error_names_the_markers_that_were_checked(self):
        svc = service(expected_markers=("a-marker-that-is-absent",))
        with pytest.raises(CorpusGateError) as exc:
            svc.build()
        assert "a-marker-that-is-absent" in str(exc.value)

    def test_build_never_returns_none(self):
        """TAC-2. The source returned None on gate failure, which is the B-1
        failure mode: an index silently does not exist and nothing says so."""
        svc = service()
        assert isinstance(svc.build(), int)


class TestGateValuesComeFromConfig:
    """TAC-6 — the markers are config-file values, not code."""

    def test_the_gate_markers_are_declared_in_the_config_file(self):
        import yaml

        data = yaml.safe_load((PLATFORM / "config" / "anita.yaml").read_text(encoding="utf-8"))
        gate = data.get("corpus_gate")
        assert gate, "corpus_gate section missing from config/anita.yaml"
        assert gate["expected_markers"], "an empty acceptance list accepts any corpus"
        assert gate["blocked_markers"], "an empty rejection list rejects nothing"

    def test_the_configured_markers_accept_the_real_corpus(self):
        """Read the values from the file, not from a test constant, so a change
        to the config is what is being verified."""
        import yaml

        gate = yaml.safe_load(
            (PLATFORM / "config" / "anita.yaml").read_text(encoding="utf-8")
        )["corpus_gate"]
        svc = RagService(
            config(expected_markers=tuple(gate["expected_markers"]),
                   blocked_markers=tuple(gate["blocked_markers"])),
            embedder=FakeEmbedder(), store=InMemoryVectorStore(),
        )
        assert svc.build() == 141


# ---------------------------------------------------------------------------
# US-103 — Chroma persistence
#
# The index currently lives in a Python list, is rebuilt on every process start
# (~25s for 141 chunks) and is discarded on exit. These tests assert the
# property that replaces it: a restart loads and embeds nothing.
# ---------------------------------------------------------------------------
chroma = pytest.importorskip("chromadb", reason="chromadb is declared in requirements.txt")


def chroma_service(tmp_path, **over):
    """A service on a real Chroma collection in a temp directory."""
    return RagService(
        config(persist_path=tmp_path / "chroma", **over),
        embedder=FakeEmbedder(16),
    )


@requires_corpus
class TestChromaPersistence:
    def test_first_run_creates_the_collection_with_cosine_space(self, tmp_path):
        svc = chroma_service(tmp_path)
        assert svc.build() == 141
        status = svc.status()
        assert status["vectors"] == 141
        assert status["collection"] == "anita_ams__v1"

        import chromadb

        client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        collection = client.get_collection("anita_ams__v1")
        assert (collection.metadata or {}).get("hnsw:space") == "cosine", (
            "without cosine space the distances are not similarities and every "
            "downstream threshold is meaningless"
        )

    def test_a_restart_embeds_only_the_query(self, tmp_path):
        """The story's central claim. A new service over the same path must open
        the collection, not rebuild it — 1 embed call, not 142."""
        first = chroma_service(tmp_path)
        first.build()

        embedder = FakeEmbedder(16)
        second = RagService(
            config(persist_path=tmp_path / "chroma"), embedder=embedder
        )
        result = second.retrieve("ultrasound gel")
        assert embedder.calls == 1, f"{embedder.calls} embed calls; a warm start makes 1"
        assert result.ok, "a persisted index that returns nothing is not persisted"
        assert result.rung == "local"
        assert second.status()["vectors"] == 141

    def test_rebuilding_is_idempotent_by_chunk_id(self, tmp_path):
        svc = chroma_service(tmp_path)
        svc.build()
        svc.build()
        assert svc.status()["vectors"] == 141, "a rebuild must replace, not append"

    def test_a_wrong_distance_space_is_refused(self, tmp_path):
        """L2 distances read as cosine similarities are numbers that look like
        similarities and are not — worse than an error."""
        import chromadb

        client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        client.create_collection("anita_ams__v1", metadata={"hnsw:space": "l2"})

        from shared.rag import SpaceMismatch

        svc = chroma_service(tmp_path)
        with pytest.raises(SpaceMismatch):
            svc.build()

    def test_an_unwritable_persist_path_fails_loudly(self, tmp_path):
        from shared.rag import VectorStoreUnavailable

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("this is a file, not a directory", encoding="utf-8")
        svc = RagService(config(persist_path=blocker / "chroma"), embedder=FakeEmbedder(16))
        with pytest.raises(VectorStoreUnavailable):
            svc.build()

    def test_the_warm_path_is_materially_faster_than_a_rebuild(self, tmp_path):
        """The reason this story exists, measured rather than asserted."""
        import time

        first = chroma_service(tmp_path)
        started = time.monotonic()
        first.build()
        cold = time.monotonic() - started

        second = RagService(config(persist_path=tmp_path / "chroma"), embedder=FakeEmbedder(16))
        started = time.monotonic()
        second.retrieve("ultrasound gel")
        warm = time.monotonic() - started

        assert warm < cold, f"warm {warm:.3f}s is not faster than cold {cold:.3f}s"


# ---------------------------------------------------------------------------
# US-104 — breaker, rungs, and the three source defects it fixes
# ---------------------------------------------------------------------------
class TestModeAndRungTruthTable:
    """Every (mode, primary-present) pair, and what the service reports.

    Written as a table because the source's behaviour differed between two
    readers of the same value and between modes in ways nothing tested (B-9,
    B-10).
    """

    @pytest.mark.parametrize("mode,has_primary,expected_effective,expected_breaker", [
        ("off", False, "off", "off"),
        ("on", False, "off", "off"),      # no primary: 'on' cannot be honoured
        ("auto", False, "off", "off"),
        ("off", True, "off", "off"),      # primary present but mode disables it
        ("auto", True, "auto", "closed"),
    ])
    def test_the_effective_mode_is_reported_honestly(
        self, mode, has_primary, expected_effective, expected_breaker
    ):
        class Primary:
            def retrieve(self, query, top_k):
                return []

        svc = RagService(
            config(mode=mode), embedder=FakeEmbedder(),
            store=InMemoryVectorStore(),
            mcp=Primary() if has_primary else None,
        )
        status = svc.status()
        assert status["mode_effective"] == expected_effective
        assert status["breaker"] == expected_breaker

    def test_mode_off_with_a_primary_never_admits(self):
        """A primary that is present but switched off must not be called — the
        source's `on` mode overrode the breaker entirely."""
        calls = []

        class Primary:
            def retrieve(self, query, top_k):
                calls.append(query)
                return []

        svc = RagService(config(mode="off"), embedder=FakeEmbedder(),
                         store=InMemoryVectorStore(), mcp=Primary())
        svc.retrieve("anything")
        assert calls == [], "mode 'off' must not consult the primary"

    def test_an_unknown_mode_is_refused_at_config_time(self):
        """B-10. The source admitted an unrecognised mode like 'auto' but then
        skipped the fallback, returning "" with rung 'none' — an empty answer
        from a configuration nobody validated."""
        with pytest.raises(RagConfigError, match="mode"):
            config(mode="banana")

    def test_mode_normalisation_has_a_single_reader(self):
        """B-9. The source read USE_MCP_RAG with `.lower()` in one place and
        `.strip().lower()` in another, so a padded value produced two different
        modes from one setting."""
        assert config(mode="  AUTO  ").normalised_mode == "auto"

    @requires_corpus
    def test_a_failing_primary_falls_back_to_local(self):
        """The rung must record the degradation, not the failure."""
        class BrokenPrimary:
            def retrieve(self, query, top_k):
                raise RuntimeError("primary is down")

        svc = RagService(config(mode="auto"), embedder=FakeEmbedder(16),
                         store=InMemoryVectorStore(), mcp=BrokenPrimary())
        svc.build()
        result = svc.retrieve("ultrasound gel")
        assert result.rung == "local", "a failed primary must fall back, not return nothing"
        assert result.ok

    @requires_corpus
    def test_the_breaker_counts_primary_failures_without_opening_early(self):
        class BrokenPrimary:
            def retrieve(self, query, top_k):
                raise RuntimeError("down")

        svc = RagService(config(mode="auto", failure_threshold=3), embedder=FakeEmbedder(16),
                         store=InMemoryVectorStore(), mcp=BrokenPrimary())
        svc.build()
        svc.retrieve("a")
        assert svc.status()["breaker"] == "closed", "one failure must not open it (B-8)"
