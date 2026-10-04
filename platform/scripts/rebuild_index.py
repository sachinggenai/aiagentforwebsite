"""Rebuild the dense index and promote it only if the frozen set passes.

This is the operational entry point US-010 and US-027 were missing.
`IndexLifecycle` could promote or reject, and `index.build_index(...,
validate=...)` could run a gate — but nothing in the product ever called
either, so a rebuild went through `rag_adapter`'s lazy path with no validation
at all. The lifecycle's atomic-promotion design was tested and unreachable.

The sequence this drives, in order:

    begin → embed → validate against the frozen set → promote, or reject

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
from anita.index import IndexLifecycle, build_index  # noqa: E402
from anita.retrieval import load_chunks  # noqa: E402


def _gate_validator(chat, gate):
    """The frozen set, run against the assembled answer engine."""

    def validate(_build) -> dict:
        report = gate.evaluate(
            lambda case: chat.post_message(
                message=case.question, session_id=f"gate-{case.case_id}"
            ).body.get("text", "")
        )
        return report.as_dict()

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
        validate=_gate_validator(chat, gate),
    )

    state = build.state.value
    print(f"build    : {build.build_id} → {state}")
    if state == "live":
        print(f"promoted : {build.collection}")
        print(f"pointer  : {store_path}")
        return 0
    print(f"rejected : {build.error or build.state.value}")
    print("the previously live index keeps serving")
    return 1


if __name__ == "__main__":
    sys.exit(main())
