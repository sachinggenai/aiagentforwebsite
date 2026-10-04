"""Pin the source voice engine's shape, so the copy stays verifiable without it.

`tests/test_mod05_voice_migration.py::test_the_engine_is_a_faithful_copy`
exists to catch an accidental rewrite of `anita/voice.py` — the turn loop's
barge-in and echo-gate tuning was derived experimentally against real phone
audio and must survive in transit.

Until CL-11 that test read the sibling repository's `app/voice_handler.py`
directly and **skipped** when that repo was absent. On a clean checkout
or in CI the engine was therefore unguarded, and a skip reads like a pass in
every run summary.

This script vendors the evidence: a manifest of the source's definitions plus
the source file's SHA-256 for provenance. The test then compares the copy
against the manifest whether or not the sibling repo exists, and warns (rather
than failing) when the live source no longer matches — the sibling is a
different product and is free to evolve; a mismatch means the manifest is
stale, not that the copy is wrong.

Regenerate after a *reviewed* change to the copy's structure:

    .venv\\Scripts\\python scripts\\voice_manifest.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent

#: Deliberately not under `data/` — that path is gitignored as a runtime
#: artefact, and this manifest is source: the guard cannot work without it.
MANIFEST = PLATFORM / "tests" / "fixtures" / "voice_engine_manifest.json"

#: The same extraction the test uses. Defined once here and asserted equal by
#: `test_the_manifest_is_present`, so the two cannot drift apart silently.
DEFINITION_RE = re.compile(r"\s*(def |class |async def )")


def definitions(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if DEFINITION_RE.match(line)]


def build_manifest(source: Path) -> dict:
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    return {
        "source_path": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_line_count": len(text.splitlines()),
        "source_definitions": definitions(text),
        "generated_on": date.today().isoformat(),
        "generated_by": "platform/scripts/voice_manifest.py",
        "note": (
            "Vendored evidence for test_the_engine_is_a_faithful_copy (CL-11). "
            "Regenerated only after a reviewed structural change to voice.py; "
            "see work-status.md §10."
        ),
    }


def _source_path(explicit: str | None) -> Path | None:
    """Where the sibling engine lives.

    Taken from the existing manifest when one is present, so the path is
    recorded in exactly one place and this script carries no sibling name of
    its own (`tests/test_vocabulary_isolation.py` scans scripts too). `--source`
    is the escape hatch for the first run or after the manifest moves.
    """
    if explicit:
        return Path(explicit)
    if MANIFEST.exists():
        recorded = json.loads(MANIFEST.read_text(encoding="utf-8")).get("source_path")
        if recorded:
            return Path(recorded)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=None,
        help="path to the source engine; defaults to the path recorded in the manifest",
    )
    args = parser.parse_args(argv)

    source = _source_path(args.source)
    if source is None:
        print("no source path: pass --source on the first run")
        return 2
    if not source.exists():
        print(f"source not found: {source}")
        print("This script only runs where the sibling repo is present.")
        return 2

    manifest = build_manifest(source)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {MANIFEST.relative_to(REPO)}")
    print(f"  source      : {manifest['source_path']}")
    print(f"  sha256      : {manifest['source_sha256'][:16]}…")
    print(f"  definitions : {len(manifest['source_definitions'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
