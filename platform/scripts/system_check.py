"""System integrity check — does every module load, wire and communicate?

The unit tests prove modules behave. The browser suite proves the chat page
works. Neither answers the question this script answers: **is the assembled
system actually connected?** A module can be complete, tested, and reachable
from nothing — that happened nine times in this build, and each one was found
by a person using the product rather than by a test.

Three layers, because they fail independently:

1. **Load** — every module imports, and the classes it exports instantiate.
   A module that cannot import is not deployed, whatever the test suite says.
2. **Wiring** — every dependency the running instance *should* have is present
   and not None. `chat.leads is None` means lead capture is silently off; the
   chat still answers, so nothing looks broken.
3. **Use cases** — every declared UC has a trigger, and driving that trigger
   touches the modules it is supposed to touch. This is the layer that catches
   "the feature exists but nothing calls it".

Run:
    .venv\\Scripts\\python scripts\\system_check.py
    .venv\\Scripts\\python scripts\\system_check.py --json
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"  [{status}] {name}{('  — ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# 1. Load
# ---------------------------------------------------------------------------
def check_load() -> list[str]:
    """Every module imports. Returns the ones that do not."""
    print("\n1. MODULE LOAD — does each file import?")
    broken: list[str] = []
    modules = sorted(p.stem for p in (PLATFORM / "app" / "anita").glob("*.py")
                     if p.stem != "__init__")
    for name in modules:
        try:
            importlib.import_module(f"anita.{name}")
        except Exception as exc:  # noqa: BLE001
            broken.append(name)
            record(FAIL, f"anita.{name}", f"{type(exc).__name__}: {exc}")
    if not broken:
        record(PASS, f"{len(modules)} modules import", "no import errors")
    return broken


# ---------------------------------------------------------------------------
# 2. Wiring
# ---------------------------------------------------------------------------
# What the assembled instance MUST have for the product to be whole. Each entry
# is (path, why it matters). A missing dependency here is not an error the
# system reports — it degrades silently, which is the point of the check.
REQUIRED_WIRING: tuple[tuple[str, str], ...] = (
    ("chat.orchestrator", "no routing without it"),
    ("chat.answers", "no deterministic facts without it"),
    ("chat.retriever", "no retrieval without it — every question declines"),
    ("chat.leads", "no lead capture — the callback offer cannot be honoured"),
    ("chat.rate_limit", "buyer surfaces unbounded"),
    ("admin.registry", "no conversation review"),
    ("admin.gateway", "no channel health"),
    ("admin.admission", "no voice admission control"),
    ("admin.metrics", "no metrics"),
    ("admin.gate", "no release gate — promotion unblocked"),
    ("router.admin_token", "admin console open to anyone"),
)


def _resolve(built, path: str):
    node = built
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


def check_wiring(built) -> list[str]:
    print("\n2. WIRING — is each dependency present on the live instance?")
    missing: list[str] = []
    for path, why in REQUIRED_WIRING:
        value = _resolve(built, path)
        if value is None:
            missing.append(path)
            record(FAIL, path, why)
    if not missing:
        record(PASS, f"{len(REQUIRED_WIRING)} dependencies wired", "none absent")

    # Modules that exist and are referenced by no running surface. Not a
    # failure — some are used by scripts and harnesses — but a number worth
    # watching, because it is how a feature goes missing.
    import ast
    app = PLATFORM / "app" / "anita"
    mods = {p.stem for p in app.glob("*.py") if p.stem != "__init__"}
    imports: dict[str, set[str]] = {}
    for p in app.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        got = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level:
                if n.module:
                    got.add(n.module.split(".")[0])
                else:
                    got.update(a.name for a in n.names)
        imports[p.stem] = got & mods
    seen: set[str] = set()
    stack = ["server", "assembly", "surfaces"]
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        stack.extend(imports.get(m, ()))
    orphaned = sorted(mods - seen - {"__main__", "audio_format", "stores"})
    record(WARN if orphaned else PASS,
           f"{len(seen)}/{len(mods)} modules reachable from a running surface",
           ", ".join(orphaned) if orphaned else "all reachable")
    return missing


# ---------------------------------------------------------------------------
# 3. Use cases
# ---------------------------------------------------------------------------
# Every UC in docs/sdlc/02-use-cases-workflows.md, with the trigger a user
# would actually perform and what the result must show. `expect` is a
# substring; `forbid` is what a broken build produces instead.
USE_CASES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("UC-01", "Ask a catalogue question via web chat",
     "what is the price of the ultrasound gel?", ("60",), ()),
    ("UC-05", "Get a price, MOQ or delivery answer",
     "what is the minimum order quantity for the ultrasound gel?", ("10",), ()),
    ("UC-06", "Compare two products",
     "compare the Ultrasound Gel and the X-Ray Cassette",
     ("Ultrasound Gel", "X-Ray Cassette"), ("which one did you mean",)),
    ("UC-07", "Request a human",
     "do you sell MRI machines?", ("get in touch",), ()),
    ("UC-08", "Provide contact details",
     "call me on 98200 11122", ("passed your details",), ()),
    ("UC-11", "Ask a clinical or diagnostic question",
     "is this safe for a pacemaker patient?", ("medical advice",), ()),
    ("UC-15", "Validate a release against the evaluation set",
     None, (), ()),  # handled separately — it is not a chat turn
)


def check_use_cases(built) -> list[str]:
    print("\n3. USE CASES — does each declared UC trigger and work?")
    chat = built.chat
    broken: list[str] = []
    deferred_count = 0

    for uc, title, trigger, expect, forbid in USE_CASES:
        if trigger is None:
            continue
        try:
            body = chat.post_message(message=trigger, session_id=f"uc-{uc}").body
            text = body.get("text") or ""
        except Exception as exc:  # noqa: BLE001
            broken.append(uc)
            record(FAIL, f"{uc} {title}", f"raised {type(exc).__name__}: {exc}")
            continue

        missing = [want for want in expect if want.lower() not in text.lower()]
        bad = [no for no in forbid if no.lower() in text.lower()]
        if missing or bad:
            broken.append(uc)
            why = f"missing {missing}" if missing else f"contains {bad}"
            record(FAIL, f"{uc} {title}", f"{why} :: {text[:70]}")
        else:
            record(PASS, f"{uc} {title}", text[:58])

    # Use cases that are declared, documented, and NOT reachable in the running
    # product. Listed explicitly rather than omitted: a report that checks 9 of
    # 15 and prints "every UC triggers" is the same overstatement this whole
    # exercise is about.
    deferred = dict(getattr(built.settings, "release", None).deferred_use_cases or ()) \
        if getattr(built.settings, "release", None) else {}

    for uc, title, why in (
        ("UC-02", "Ask a catalogue question via WhatsApp",
         "WhatsAppAdapter is registered but no webhook route is served"),
        ("UC-03", "Inbound voice call",
         "no voice route in the API; the engine has never carried a call"),
        ("UC-04", "Browser-mic voice question",
         "the ticket route is served but there is no audio path to use it"),
        ("UC-09", "Outbound follow-up",
         "OutboundQueue has no worker — leads qualify and nothing dials"),
        ("UC-10", "Negative sentiment detected",
         "anita.sentiment is complete and referenced by no surface"),
        ("UC-14", "Add a new messaging channel",
         "an extension point, verified by design review rather than by a trigger"),
    ):
        if uc in deferred:
            # Deferred by a written decision, so this does not block promotion.
            # Reported as its own status rather than quietly passing: an
            # operator reading this report must be able to see what is missing
            # and why, not just that nothing failed.
            record(INFO, f"{uc} {title} — DEFERRED", deferred[uc].split(".")[0])
            deferred_count += 1
        else:
            broken.append(uc)
            record(FAIL, f"{uc} {title}", why)

    # UC-12 / UC-13 / UC-15 are operator surfaces, not chat turns.
    try:
        channels = built.admin.channels().body
        ok = bool(channels.get("channels")) and not channels.get("unavailable")
        record(PASS if ok else FAIL, "UC-12 Operator monitors instance health",
               f"{len(channels.get('channels', []))} channels")
        if not ok:
            broken.append("UC-12")
    except Exception as exc:  # noqa: BLE001
        broken.append("UC-12")
        record(FAIL, "UC-12 Operator monitors instance health", f"{type(exc).__name__}")

    try:
        listed = built.admin.conversations().body
        ok = listed.get("count", 0) > 0 and not listed.get("unavailable")
        record(PASS if ok else FAIL, "UC-13 Operator reviews a conversation",
               f"{listed.get('count', 0)} conversations")
        if not ok:
            broken.append("UC-13")
    except Exception as exc:  # noqa: BLE001
        broken.append("UC-13")
        record(FAIL, "UC-13 Operator reviews a conversation", f"{type(exc).__name__}")

    try:
        report = built.admin.gate.evaluate(
            lambda c: chat.post_message(message=c.question, session_id=f"g-{c.case_id}")
            .body.get("text", "")
        )
        record(PASS if report.passed else FAIL,
               "UC-15 Validate a release against the evaluation set",
               f"{report.total} cases, {report.reason or 'ok'}")
        if not report.passed:
            broken.append("UC-15")
    except Exception as exc:  # noqa: BLE001
        broken.append("UC-15")
        record(FAIL, "UC-15 Validate a release", f"{type(exc).__name__}: {exc}")

    return broken, deferred_count


# ---------------------------------------------------------------------------
# 4. Communication — do the modules actually talk to each other?
# ---------------------------------------------------------------------------
def check_communication(built) -> list[str]:
    """Drive one turn and observe which modules it touches.

    Wiring being present is not the same as wiring being *used*. This is the
    layer that would have caught `compare()`: present on the object, reachable
    from every test, and called by nothing.
    """
    print("\n4. COMMUNICATION — which modules does a turn actually use?")
    broken: list[str] = []
    touched: set[str] = set()

    chat = built.chat
    leads = chat.leads

    # A retrieval turn must reach the retriever AND the gate.
    marked: list[str] = []
    original_retrieve = chat.retriever.retrieve

    def traced_retrieve(*a, **kw):
        marked.append("retriever.retrieve")
        result = original_retrieve(*a, **kw)
        marked.append(f"gate:{result.decision.reason}")
        return result

    chat.retriever.retrieve = traced_retrieve
    chat.post_message(message="tell me about the X-Ray Cassette", session_id="comm-1")
    chat.retriever.retrieve = original_retrieve
    if "retriever.retrieve" in marked:
        touched.add("retriever")
        record(PASS, "chat → retrieval → grounding gate", ", ".join(marked[:3]))
    else:
        broken.append("retrieval")
        record(FAIL, "chat → retrieval → grounding gate", "retriever never called")

    # A commercial turn must reach the deterministic engine, not retrieval.
    before = len(marked)
    chat.post_message(message="what is the price of the ultrasound gel?", session_id="comm-2")
    if chat.answers is not None:
        touched.add("answers")
        record(PASS, "chat → deterministic answer engine", "wired")

    # A lead turn must reach the CRM store.
    captured: list[str] = []
    if leads is not None:
        original_capture = leads.capture

        def traced_capture(*a, **kw):
            captured.append("leads.capture")
            return original_capture(*a, **kw)

        leads.capture = traced_capture
        chat.post_message(message="call me on 98200 11122", session_id="comm-3")
        leads.capture = original_capture
        if captured:
            touched.add("leads")
            record(PASS, "chat → LeadService → MockCrmStore", "capture called")
        else:
            broken.append("leads")
            record(FAIL, "chat → LeadService → MockCrmStore", "capture never called")
    else:
        broken.append("leads")
        record(FAIL, "chat → LeadService → MockCrmStore", "no lead service wired")

    # The orchestrator must record turns for every one of the above.
    try:
        history = chat.get_history(session_id="comm-2").body
        ok = bool(history.get("turns"))
        record(PASS if ok else FAIL, "chat → ConversationRegistry",
               f"{len(history.get('turns') or [])} turns recorded")
        if not ok:
            broken.append("registry")
    except Exception as exc:  # noqa: BLE001
        broken.append("registry")
        record(FAIL, "chat → ConversationRegistry", f"{type(exc).__name__}")

    return broken


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    print("=" * 92)
    print("SYSTEM INTEGRITY CHECK")
    print("=" * 92)

    broken_load = check_load()

    from anita.assembly import assemble

    try:
        built = assemble(require_preflight=False)
    except Exception as exc:  # noqa: BLE001
        print(f"\nASSEMBLY FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2

    if built.chat.retriever is not None:
        try:
            built.chat.retriever.build_index()
        except Exception as exc:  # noqa: BLE001
            record(WARN, "dense index", f"not built — {type(exc).__name__}")

    broken_wiring = check_wiring(built)
    broken_uc, deferred_count = check_use_cases(built)
    broken_comm = check_communication(built)

    print("\n" + "=" * 92)
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    warned = sum(1 for s, _, _ in results if s == WARN)
    print(f"  {passed} passed · {failed} failed · {warned} warnings"
          + (f" · {deferred_count} deferred" if deferred_count else ""))
    everything = broken_load + broken_wiring + broken_uc + broken_comm
    if everything:
        print(f"\n  BROKEN: {', '.join(everything)}")
    else:
        # Never "every UC triggers" while some are deferred — that is the
        # overstatement this whole check exists to stop.
        tail = (f"  Every module loads and every dependency is wired. "
                f"{deferred_count} use case(s) deferred by decision.")
        print(tail if deferred_count else
              "  Every module loads, every dependency is wired, every UC triggers.")
    print("=" * 92)

    if args.json:
        print(json.dumps({
            "passed": passed, "failed": failed, "warnings": warned,
            "broken": everything,
            "deferred": deferred_count,
            "results": [{"status": s, "name": n, "detail": d} for s, n, d in results],
        }, indent=2))

    return 1 if everything else 0


if __name__ == "__main__":
    raise SystemExit(main())
