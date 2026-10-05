"""The release gate — one promotion decision from both evidence sources.

There are two verification systems in this repository and until now only one of
them decided anything:

* The **evaluation set** (`anita.observability`) runs in-process in under a
  second and asserts answer content. It is what `IndexLifecycle.promote`
  consumes. It could not see routing, refusals, consent capture, or whether the
  page works at all — the failures that actually shipped.
* The **browser suite** (`tests/e2e`) drives the real page and asserts what a
  buyer sees. It saw everything and decided nothing.

This script runs both and produces a single verdict. `combine_validation`
treats an unrun browser suite as a failure, so "we did not check" and "the
check passed" cannot produce the same promotion.

Run:
    .venv\\Scripts\\python scripts\\release_gate.py              # both
    .venv\\Scripts\\python scripts\\release_gate.py --no-browser # eval set only
    .venv\\Scripts\\python scripts\\release_gate.py --json       # machine-readable

Exit codes: 0 all three passed · 1 any failed · 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

E2E_DIR = PLATFORM / "tests" / "e2e"
REPORT_PATH = PLATFORM.parent / "data" / "out" / "reports" / "release-gate.json"

#: Where the browser suite expects a running instance. `run.sh` starts
#: Streamlit here; `ANITA_E2E_URL` points the suite at a different one.
DEFAULT_E2E_PORT = "8085"


def instance_url() -> str:
    return os.environ.get(
        "ANITA_E2E_URL", f"http://127.0.0.1:{os.environ.get('ANITA_E2E_PORT', DEFAULT_E2E_PORT)}"
    ).rstrip("/")


def instance_is_up(url: str | None = None, *, timeout: float = 2.0) -> bool:
    """True when something is serving the instance's health endpoint.

    The browser suite drives a *running instance*, and the gate used to run it
    blind. With nothing listening, Playwright's first test hit
    `ERR_CONNECTION_REFUSED`, the serial spec skipped the other 49, and the
    gate reported `1 scenario failed` — blaming the product for an environment
    problem, which is the exact confusion this script exists to prevent
    (work-status §8.19).
    """
    import urllib.request

    target = f"{(url or instance_url())}/_stcore/health"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - unreachable in any way is unreachable
        return False


def run_evaluation_set() -> tuple[object, dict]:
    """The in-process evaluation set. Returns (GateReport, validation dict)."""
    from anita.assembly import assemble, deployed_environment
    from anita.observability import cases_from_corpus

    built = assemble(require_preflight=False, env=deployed_environment())
    chat = built.chat
    if chat.retriever is not None:
        try:
            chat.retriever.build_index()
        except Exception as exc:  # noqa: BLE001 - report, do not crash the gate
            print(f"  warning: dense index not built — {type(exc).__name__}: {exc}")

    gate = built.admin.gate
    if gate is None:
        # No cases is not a pass. A gate with nothing to assert certifies
        # nothing, and reporting success would be the worst outcome here.
        from anita.observability import GateReport

        return GateReport(passed=False, total=0,
                          reason="no evaluation cases — the gate has nothing to assert"), {}

    report = gate.evaluate(
        lambda case: chat.post_message(
            message=case.question, session_id=f"gate-{case.case_id}"
        ).body.get("text", "")
    )

    # US-012, `TAC-1`/`TAC-2`: a grounded answer with no citation, or a
    # citation that could not be completed, is a composition defect — and the
    # only way to know is to look at every reply the frozen set produced. The
    # gate above sees text; this sees provenance.
    integrity = {"grounded": 0, "uncited": 0, "incomplete": 0}
    try:
        for case in gate.cases:
            body = chat.post_message(
                message=case.question, session_id=f"gate-cite-{case.case_id}"
            ).body
            if not body.get("grounded"):
                continue
            integrity["grounded"] += 1
            if not body.get("grounded_on"):
                integrity["uncited"] += 1
            if body.get("citation_incomplete"):
                integrity["incomplete"] += 1
    except Exception as exc:  # noqa: BLE001 - an integrity run that broke is not a pass
        integrity["error"] = f"{type(exc).__name__}: {exc}"

    validation = report.as_dict()
    validation["citations"] = integrity
    return report, validation


def run_browser_suite(project: str | None = None) -> "BrowserReport":
    """The Playwright suite, as a subprocess."""
    from anita.observability import BrowserReport

    if not (E2E_DIR / "node_modules").exists():
        return BrowserReport(
            passed=False, total=0,
            reason=f"Playwright is not installed in {E2E_DIR} — run `npm install`",
        )

    url = instance_url()
    if not instance_is_up(url):
        return BrowserReport(
            passed=False, total=0,
            reason=(
                f"no instance on {url} — this is an environment problem, not a "
                f"product failure. Start one (`bash tests/e2e/run.sh`) or point "
                f"ANITA_E2E_URL at a running instance, then re-run the gate."
            ),
        )

    command = ["npx", "playwright", "test", "--reporter=json"]
    if project:
        command.append(f"--project={project}")

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=E2E_DIR, capture_output=True, text=True, timeout=1800,
            shell=(sys.platform == "win32"),  # npx is a .cmd shim on Windows
        )
    except subprocess.TimeoutExpired:
        return BrowserReport(passed=False, reason="browser suite timed out after 30 minutes")
    except FileNotFoundError:
        return BrowserReport(passed=False, reason="npx not found — is Node installed?")

    duration = time.monotonic() - started

    # Playwright writes the JSON report to stdout. Anything else on stdout is
    # noise from the runner, so parse from the first brace.
    payload = {}
    text = completed.stdout or ""
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            payload = {}

    if not payload:
        return BrowserReport(
            passed=False, duration_s=duration,
            reason=f"could not read the Playwright report (exit {completed.returncode})",
        )

    failed: list[str] = []
    total = 0

    def walk(suite: dict) -> None:
        nonlocal total
        for spec in suite.get("specs", []):
            total += 1
            if spec.get("ok") is False:
                failed.append(spec.get("title", "<untitled>"))
        for child in suite.get("suites", []):
            walk(child)

    for suite in payload.get("suites", []):
        walk(suite)

    passed = bool(payload.get("stats", {}).get("expected")) and not failed
    return BrowserReport(
        passed=passed,
        total=total,
        failed=tuple(failed),
        duration_s=duration,
        reason="" if passed else f"{len(failed)} scenario(s) failed",
    )


def run_system_check() -> "SystemReport":
    """The system integrity check, as a subprocess.

    Run separately rather than in this process because it assembles its own
    instance and deliberately drives it — the point is to observe a real
    instance, not the one this script happens to be holding.
    """
    from anita.observability import SystemReport

    script = PLATFORM / "scripts" / "system_check.py"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return SystemReport(passed=False, reason="system check timed out")

    payload = {}
    text = completed.stdout or ""
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            payload = {}

    if not payload:
        return SystemReport(
            passed=False,
            reason=f"could not read the system check report (exit {completed.returncode})",
        )

    broken = tuple(payload.get("broken") or ())
    loaded = wired = 0
    for row in payload.get("results", []):
        if row.get("name", "").endswith("modules import"):
            loaded = int(str(row.get("name", "0")).split()[0] or 0)
        if row.get("name", "").endswith("dependencies wired"):
            wired = int(str(row.get("name", "0")).split()[0] or 0)

    return SystemReport(
        passed=not broken,
        loaded=loaded,
        wired=wired,
        broken=broken,
        reason="" if not broken else f"{len(broken)} unreachable: {', '.join(broken[:8])}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true",
                        help="skip the browser suite; the result will NOT permit promotion")
    parser.add_argument("--no-system", action="store_true",
                        help="skip the system check; the result will NOT permit promotion")
    parser.add_argument("--project", default=None,
                        help="restrict the browser suite to one Playwright project")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    print("=" * 88)
    print("RELEASE GATE")
    print("=" * 88)

    from anita.observability import combine_validation

    print("\n1. Evaluation set (in-process)")
    gate_report, gate_validation = run_evaluation_set()
    status = "PASS" if gate_report.passed else "FAIL"
    print(f"   [{status}] {gate_report.total} cases — {gate_report.reason or 'ok'}")
    for case_id in gate_report.failed_ids[:10]:
        print(f"          failed: {case_id}")

    # US-012: 100 % of grounded answers must cite something, and no citation
    # may be incomplete. Both counts are printed even at zero — a check that
    # only speaks when it fails cannot be told from one that never ran.
    citations = gate_validation.get("citations", {})
    if citations:
        cite_ok = not (citations.get("uncited") or citations.get("incomplete")
                       or citations.get("error"))
        cite_status = "PASS" if cite_ok else "FAIL"
        print(f"   [{cite_status}] citation integrity — "
              f"{citations.get('grounded', 0)} grounded answers, "
              f"{citations.get('uncited', 0)} uncited, "
              f"{citations.get('incomplete', 0)} incomplete")
        if citations.get("error"):
            print(f"          {citations['error']}")

    browser_report = None
    if args.no_browser:
        print("\n2. Browser suite — SKIPPED (--no-browser)")
        print("   Promotion will be refused: an unrun suite is not evidence.")
    else:
        print("\n2. Browser suite (Playwright)")
        browser_report = run_browser_suite(args.project)
        # A suite that never ran is not a failed suite. The verdict below still
        # refuses promotion (an unrun suite is not evidence), but the line must
        # not read as a product failure.
        if browser_report.total == 0:
            status = "NOT RUN"
        else:
            status = "PASS" if browser_report.passed else "FAIL"
        print(f"   [{status}] {browser_report.total} scenarios in "
              f"{browser_report.duration_s:.0f}s — {browser_report.reason or 'ok'}")
        for title in list(browser_report.failed)[:10]:
            print(f"          failed: {title}")

    system_report = None
    if args.no_system:
        print("\n3. System integrity — SKIPPED (--no-system)")
        print("   Promotion will be refused: an unrun check is not evidence.")
    else:
        print("\n3. System integrity (load · wiring · use cases · communication)")
        system_report = run_system_check()
        status = "PASS" if system_report.passed else "FAIL"
        print(f"   [{status}] {system_report.loaded} modules loaded, "
              f"{system_report.wired} dependencies wired")
        for item in list(system_report.broken)[:10]:
            print(f"          unreachable: {item}")

    # §8.24: the citation counts travel with the verdict. They used to be
    # printed and then dropped here, so a provenance regression was visible in
    # the log and could not refuse a promotion.
    validation = combine_validation(
        gate_report, browser_report, system_report, citations=citations
    )

    print("\n" + "=" * 88)
    print(f"  RESULT: {'PASS — promotion permitted' if validation['passed'] else 'FAIL — promotion refused'}")
    print(f"  {validation['reason']}")
    print("=" * 88)

    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(validation, indent=2), encoding="utf-8")
        if not args.json:
            print(f"report -> {REPORT_PATH}")
    except OSError as exc:
        print(f"could not write the report: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(validation, indent=2))

    return 0 if validation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
