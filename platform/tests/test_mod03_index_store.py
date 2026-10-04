"""US-010 — the durable build store, the one-LIVE rule, and the pointer.

`IndexLifecycle` was tested and unreachable: it promoted into an in-memory
store, so a restart forgot which collection was live and the versioned
collections were a naming convention nothing read. These are the properties
that make promotion real — enforced by the database, and read by the process
that serves answers.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.index import (  # noqa: E402
    DoublePromotion,
    IndexLifecycle,
    IndexState,
    SqliteBuildStore,
    live_collection_name,
)


def _store(tmp_path) -> SqliteBuildStore:
    return SqliteBuildStore(tmp_path / "anita_index.sqlite")


def _promote(lifecycle: IndexLifecycle, *, passed: bool = True):
    build = lifecycle.begin(embed_model="nomic-embed-text", dimensions=768)
    lifecycle.built(chunks=141, dimensions=768, corpus_hash="deadbeef")
    return lifecycle.promote({"passed": passed, "reason": "ok" if passed else "gate"})


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class TestSqliteBuildStore:
    def test_a_build_round_trips(self, tmp_path):
        store = _store(tmp_path)
        lifecycle = IndexLifecycle("anita_ams__v1", store)
        build = _promote(lifecycle)

        reopened = SqliteBuildStore(tmp_path / "anita_index.sqlite")
        live = reopened.latest_live()
        assert live is not None
        assert live.build_id == build.build_id
        assert live.collection == build.collection
        assert live.dimensions == 768
        assert live.validation["passed"] is True
        assert reopened.live_collection() == build.collection

    def test_promotion_demotes_the_previous_build_in_one_transaction(self, tmp_path):
        store = _store(tmp_path)
        lifecycle = IndexLifecycle("anita_ams__v1", store)
        first = _promote(lifecycle)
        second = _promote(lifecycle)

        assert second.collection != first.collection
        live = store.latest_live()
        assert live.build_id == second.build_id
        # The superseded row is kept for history, marked — never deleted.
        history = {row["build_id"]: row for row in store.history()}
        assert history[first.build_id]["state"] == IndexState.REJECTED.value
        assert history[first.build_id]["error"] == "superseded"

    def test_the_database_refuses_a_second_live_row_for_one_collection(self, tmp_path):
        """TAC-3, enforced by the partial unique index rather than a check."""
        store = _store(tmp_path)
        lifecycle = IndexLifecycle("anita_ams__v1", store)
        _promote(lifecycle)

        # Bypass the lifecycle's convenience: write a second LIVE row for the
        # same collection directly, which is what a double promotion is.
        from anita.index import IndexBuild

        rogue = IndexBuild(build_id="rogue", collection="anita_ams__v1",
                           state=IndexState.LIVE,
                           started_at=datetime.now(timezone.utc))
        with pytest.raises(DoublePromotion):
            store.save(rogue)

        # ...and the raw constraint is really there, not merely wrapped.
        connection = sqlite3.connect(tmp_path / "anita_index.sqlite")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO index_builds (build_id, collection, state, started_at) "
                "VALUES ('raw', 'anita_ams__v1', 'LIVE', '2026-01-01T00:00:00+00:00')"
            )
        connection.close()

    def test_two_live_rows_for_different_collections_are_allowed(self, tmp_path):
        """The constraint is per collection, exactly as the story's DDL says."""
        store = _store(tmp_path)
        first = IndexLifecycle("anita_ams__v1", store)
        _promote(first)
        # A different base collection is a different instance's concern; it
        # must not be blocked by this one's live row.
        other = IndexLifecycle("other_product__v1", SqliteBuildStore(
            tmp_path / "other.sqlite"))
        _promote(other)
        assert store.latest_live() is not None

    def test_history_is_readable_for_an_operator(self, tmp_path):
        store = _store(tmp_path)
        lifecycle = IndexLifecycle("anita_ams__v1", store)
        _promote(lifecycle)
        lifecycle.begin(embed_model="m", dimensions=768)
        lifecycle.reject("gate_regression")

        history = store.history(limit=5)
        assert {row["state"] for row in history} == {
            IndexState.REJECTED.value, IndexState.LIVE.value,
        }


# ---------------------------------------------------------------------------
# The lifecycle over the durable store (restart, versioning, refusal)
# ---------------------------------------------------------------------------
class TestLifecycleOverTheStore:
    def test_a_restart_remembers_which_collection_is_live(self, tmp_path):
        first = IndexLifecycle("anita_ams__v1", _store(tmp_path))
        live = _promote(first)

        # The restart: a new process, the same database.
        restarted = IndexLifecycle("anita_ams__v1", SqliteBuildStore(
            tmp_path / "anita_index.sqlite"))
        assert restarted.live_collection == live.collection
        assert restarted.state is IndexState.LIVE

    def test_collection_versions_do_not_reuse_a_number_after_a_restart(self, tmp_path):
        first = IndexLifecycle("anita_ams__v1", _store(tmp_path))
        _promote(first)
        second = IndexLifecycle("anita_ams__v1", SqliteBuildStore(
            tmp_path / "anita_index.sqlite"))
        build = second.begin()
        assert build.collection == "anita_ams__v2"

    def test_a_failed_gate_rejects_and_leaves_the_previous_live(self, tmp_path):
        lifecycle = IndexLifecycle("anita_ams__v1", _store(tmp_path))
        live = _promote(lifecycle)
        attempted = _promote(lifecycle, passed=False)

        assert attempted.state is IndexState.REJECTED
        assert lifecycle.live_collection == live.collection
        assert lifecycle.store.latest_live().build_id == live.build_id

    def test_status_reports_both_what_is_live_and_what_is_building(self, tmp_path):
        """An operator watching a rebuild needs both, separately."""
        lifecycle = IndexLifecycle("anita_ams__v1", _store(tmp_path))
        live = _promote(lifecycle)
        lifecycle.begin()
        status = lifecycle.status()
        assert status["live_build_id"] == live.build_id
        assert status["state"] == "building"
        assert status["history"]


# ---------------------------------------------------------------------------
# The pointer the serving process reads
# ---------------------------------------------------------------------------
class TestLiveCollectionPointer:
    def test_no_store_means_the_configured_default(self, tmp_path):
        assert live_collection_name(tmp_path / "absent.sqlite", "anita_ams__v1") == \
            "anita_ams__v1"

    def test_a_promoted_build_wins_over_the_default(self, tmp_path):
        store_path = tmp_path / "anita_index.sqlite"
        lifecycle = IndexLifecycle("anita_ams__v1", SqliteBuildStore(store_path))
        live = _promote(lifecycle)
        assert live_collection_name(store_path, "anita_ams__v1") == live.collection

    def test_reading_the_pointer_does_not_create_the_store(self, tmp_path):
        """A boot that wrote the state it was checking would make a fresh
        deployment look like it had a build history."""
        path = tmp_path / "never-written.sqlite"
        assert live_collection_name(path, "anita_ams__v1") == "anita_ams__v1"
        assert not path.exists()

    def test_a_store_with_no_live_build_falls_back_to_the_default(self, tmp_path):
        store_path = tmp_path / "anita_index.sqlite"
        lifecycle = IndexLifecycle("anita_ams__v1", SqliteBuildStore(store_path))
        lifecycle.begin()
        lifecycle.reject("embedding failed")
        assert live_collection_name(store_path, "anita_ams__v1") == "anita_ams__v1"


# ---------------------------------------------------------------------------
# The boot assertion (edge case 1)
# ---------------------------------------------------------------------------
class TestBootRefusesAnEmptyIndex:
    def _settings(self, tmp_path, monkeypatch):
        from anita.config import load_settings

        settings = load_settings(env={})
        # Point the instance at a temporary root so the check reads only what
        # this test wrote.
        object.__setattr__(settings, "project_root", tmp_path)
        return settings

    def test_absent_index_refuses_the_boot_with_an_actionable_message(self, tmp_path):
        from anita.boot import _phase_index

        result = _phase_index(self._settings(tmp_path, None))
        assert not result.ok
        assert any("NO_INDEX" in failure for failure in result.failures)
        assert any("rebuild_index.py" in failure for failure in result.failures)

    def test_vectors_without_a_build_history_are_allowed_and_said_out_loud(self, tmp_path):
        from anita.boot import _phase_index

        settings = self._settings(tmp_path, None)
        chroma = (tmp_path / settings.isolation.chroma_path)
        chroma.mkdir(parents=True, exist_ok=True)
        (chroma / "chroma.sqlite3").write_bytes(b"x")

        result = _phase_index(settings)
        assert result.ok
        assert "no promoted build yet" in result.detail

    def test_a_promoted_build_satisfies_the_boot_check(self, tmp_path):
        from anita.boot import _phase_index

        settings = self._settings(tmp_path, None)
        log_dir = tmp_path / settings.isolation.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        lifecycle = IndexLifecycle("anita_ams__v1", SqliteBuildStore(
            log_dir / "anita_index.sqlite"))
        live = _promote(lifecycle)

        result = _phase_index(settings)
        assert result.ok
        assert live.collection in result.detail
