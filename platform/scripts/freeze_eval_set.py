"""Freeze the evaluation set to a versioned repo file (US-027, TAC-1).

The release gate used to regenerate its cases from the catalogue at every boot.
That meant a corpus refresh silently changed the test set — the opposite of
"frozen" — and it made every "70/70 passed" a statement about a different set
than the previous one.

This writes the set to `eval/anita_frozen_set.jsonl` and prints its SHA-256, so
the hash can be pinned in `config/anita.yaml` (release.eval_set_sha256) as a
**reviewed** act. Regeneration is never automatic: a corpus change that moves
the set is a decision about what the product promises, and the diff below is
what makes it a decision rather than drift.

    .venv\\Scripts\\python scripts\\freeze_eval_set.py            # dry run: shows the diff
    .venv\\Scripts\\python scripts\\freeze_eval_set.py --write    # write + print the new hash
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

# Windows consoles default to cp1252 and this report uses "→" and "…"; without
# this the work succeeds and the summary of it crashes (the same guard
# `serve.py` and `release_gate.py` carry).
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from anita.config import load_settings  # noqa: E402
from anita.corpus import Corpus  # noqa: E402
from anita.observability import (  # noqa: E402
    cases_from_corpus,
    eval_set_sha256,
    load_eval_set,
)


def _serialise(cases, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(case.as_dict(), ensure_ascii=False)
        for case in sorted(cases, key=lambda case: case.case_id)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true",
        help="write the file; without it this is a dry run",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    path = settings.eval_set_path
    cases = cases_from_corpus(Corpus(settings.corpus_dir))

    existing = load_eval_set(path) if path.exists() else []
    old_ids = {case.case_id for case in existing}
    new_ids = {case.case_id for case in cases}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)

    print(f"set       : {path.relative_to(PLATFORM)}")
    print(f"generated : {len(cases)} cases from the catalogue")
    if existing:
        print(f"current   : {len(existing)} cases in the frozen file")
        print(f"added     : {len(added)} {added[:5]}{' …' if len(added) > 5 else ''}")
        print(f"removed   : {len(removed)} {removed[:5]}{' …' if len(removed) > 5 else ''}")

    if not args.write:
        print(
            "\ndry run — nothing written.\n"
            "Re-run with --write, review the diff, then pin the printed hash in\n"
            "config/anita.yaml → release.eval_set_sha256."
        )
        return 0

    _serialise(cases, path)
    print(f"\nwrote     : {path.relative_to(PLATFORM)}")
    print(f"sha256    : {eval_set_sha256(path)}")
    print("pin it    : config/anita.yaml → release.eval_set_sha256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
