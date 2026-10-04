"""Run every validation gate and write the report.

Exits non-zero if any gate fails, so this can gate a build.

Usage:
    python scripts/05_validate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.config import force_utf8, load_config  # noqa: E402
from ams.validate import emit_report, validate  # noqa: E402


def main() -> int:
    force_utf8()
    config = load_config()
    report = validate(config)
    path = emit_report(config, report)

    print(f"{'GATE':26} {'STATUS':6} DETAIL")
    print("-" * 92)
    for gate in report.gates:
        print(f"{gate.name:26} {gate.status:6} {gate.detail}")

    failed = [g for g in report.gates if not g.passed]
    if failed:
        print(f"\n{len(failed)} gate(s) FAILED:")
        for gate in failed:
            print(f"\n  {gate.name}:")
            for item in gate.failures[:10]:
                print(f"    - {item}")
            if len(gate.failures) > 10:
                print(f"    ... and {len(gate.failures) - 10} more")

    print(f"\n{'OVERALL: PASS' if report.passed else 'OVERALL: FAIL'}")
    print(f"report -> {path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
