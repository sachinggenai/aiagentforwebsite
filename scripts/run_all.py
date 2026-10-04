"""Run the whole pipeline: discover -> fetch -> parse -> emit -> validate.

Usage:
    python scripts/run_all.py                 # full run, uses the cache
    python scripts/run_all.py --force-fetch   # re-fetch everything
    python scripts/run_all.py --skip-crawl    # parse/emit/validate from cache
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def run(script: str, *args: str) -> int:
    """Run a pipeline stage, streaming its output."""
    command = [sys.executable, str(SCRIPTS / script), *args]
    print(f"\n{'=' * 78}\n  {script} {' '.join(args)}\n{'=' * 78}")
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603
    elapsed = time.monotonic() - started
    print(f"  -> exit {result.returncode} in {elapsed:.1f}s")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-fetch", action="store_true")
    parser.add_argument("--skip-crawl", action="store_true")
    args = parser.parse_args()

    stages: list[tuple[str, tuple[str, ...]]] = []
    if not args.skip_crawl:
        stages.append(("01_discover.py", ()))
        stages.append(("02_fetch.py", ("--force",) if args.force_fetch else ()))
    stages += [
        ("03_parse.py", ()),
        ("04_emit.py", ()),
        ("05_validate.py", ()),
    ]

    for script, extra in stages:
        code = run(script, *extra)
        if code != 0:
            print(f"\nPipeline stopped: {script} exited {code}")
            return code

    print(f"\n{'=' * 78}")
    print("  Pipeline complete — all stages passed")
    print(f"{'=' * 78}")
    print(f"\nOutputs in: {ROOT / 'data' / 'out'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
