"""Rebuild the dense index and promote it only if the frozen set passes.

This is the operational entry point US-010 and US-027 were missing.
`IndexLifecycle` could promote or reject, and `index.build_index(...,
validate=...)` could run a gate — but nothing in the product ever called
either, so a rebuild went through `rag_adapter`'s lazy path with no validation
at all. The lifecycle's atomic-promotion design was tested and unreachable.

The sequence this drives, in order:

    begin → embed → validate against the frozen set → promote, or reject

Both the embed and the validation target the collection the build **names**, not
the one that is live while the rebuild runs: `index.staging_retriever()` binds
the write to the new collection, and the gate is answered through a retriever
bound to the same one. Before that, a second rebuild validated the previous
index and promoted a collection nothing had written (`work-status.md` §8.23).

A rejection leaves the previously live index serving: a refresh that goes wrong
is a no-op rather than an outage (`SM-05`). The validation report carries the
frozen set's SHA-256, so a promoted build can be tied to the exact question set
it passed (TAC-1).

    .venv\\Scripts\\python scripts\\rebuild_index.py              # rebuild + validate + promote
    .venv\\Scripts\\python scripts\\rebuild_index.py --dry-run    # validate what is live, change nothing

Exit codes: 0 promoted (or dry-run pass) · 1 rejected · 2 could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from anita.assembly import assemble, deployed_environment  # noqa: E402
from anita.index import IndexLifecycle, build_index, staging_retriever  # noqa: E402
from anita.retrieval import load_chunks  # noqa: E402


def _gate_validator(chat, gate, retriever):
    """The frozen set, run against the build that is about to be promoted.

    US-010: the gate must score the **staging** collection. The answer path
    reads through `chat.retriever`, so for each build the surface is pointed at
    a sibling bound to `build.collection` and restored afterwards — on failure
    too, so a rejected build cannot leave this process answering from a
    collection that was never promoted.

    §8.33: the verdict carries **provenance** as well as text. The gate alone
    asserts answer content and never sees citations, so before this a rebuild
    could be promoted while uncited or incomplete answers existed — the one
    moment `citation_failure` was written for. It reuses the release gate's own
    counts and rule rather than re-deriving them, because a second copy of the
    rule is a second place for it to drift.
    """

    def validate(build) -> dict:
        from anita.observability import citation_failure, citation_integrity

        staging = staging_retriever(retriever, build.collection)
        previous = chat.retriever
        chat.retriever = staging
        try:
            report = gate.evaluate(
                lambda case: chat.post_message(
                    message=case.question, session_id=f"gate-{case.case_id}"
                ).body.get("text", "")
            )
            # A pass of its own, so this does not disturb the text pass's
            # conversations — the same separation the release gate makes.
            citations = citation_integrity(chat, gate.cases, session_prefix="gate-cite")
        finally:
            chat.retriever = previous

        validation = report.as_dict()
        validation["citations"] = citations
        failure = citation_failure(citations)
        if failure:
            validation["passed"] = False
            validation["reason"] = failure
        return validation

    return validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate the live index without rebuilding or promoting",
    )
    args = parser.parse_args(argv)

    built = assemble(require_preflight=False, env=deployed_environment())
    if built.admin.gate is None:
        print("refused: the release gate is unavailable — nothing may be promoted")
        for note in built.notes:
            print(f"  - {note}")
        return 2

    gate = built.admin.gate
    chat = built.chat
    print(f"eval set : {built.settings.release.eval_set}")
    print(f"set hash : {gate.set_sha256[:16]}…  ({len(gate.cases)} cases)")

    if args.dry_run:
        report = gate.evaluate(
            lambda case: chat.post_message(
                message=case.question, session_id=f"gate-{case.case_id}"
            ).body.get("text", "")
        )
        print(f"verdict  : {'PASS' if report.passed else 'BLOCK'} — {report.reason}")
        return 0 if report.passed else 1

    retriever = chat.retriever
    if retriever is None:
        print("refused: no retriever in this assembly — nothing to rebuild")
        return 2

    chunks = load_chunks(built.settings.corpus_dir / built.settings.corpus.chunks_file)

    # US-010: the durable store, so the promotion survives this process and the
    # serving processes read the same pointer. The class name records the
    # intent; without it the lifecycle's versioning was in-memory and a restart
    # forgot which collection was live.
    from anita.index import SqliteBuildStore

    store_path = built.settings.log_dir_path / "anita_index.sqlite"
    lifecycle = IndexLifecycle(
        built.settings.isolation.chroma_collection,
        SqliteBuildStore(store_path),
    )
    print(f"collections: next {lifecycle.collection_for(lifecycle._next_version)}, "
          f"live {lifecycle.live_collection or 'none'}")

    build = build_index(
        lifecycle,
        chunks,
        retriever=retriever,
        validate=_gate_validator(chat, gate, retriever),
    )

    state = build.state.value
    print(f"build    : {build.build_id} → {state}")
    if state == "live":
        print(f"promoted : {build.collection}  ({build.chunks} vectors embedded and validated)")
        print(f"pointer  : {store_path}")
        return 0
    print(f"rejected : {build.error or build.state.value}")
    print("the previously live index keeps serving")
    return 1


if __name__ == "__main__":
    sys.exit(main())
