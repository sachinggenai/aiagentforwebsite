"""Trace every use case's trigger through the subsystems that should serve it.

`trace_question.py` shows one question in full. This runs the whole set and
prints the comparison that matters: **which subsystems each trigger was
supposed to reach, and which it actually reached.**

The catalogue bug is the reason this exists. "Show me the catalogue" was
classified as a product question and never called `AnitaAnswers.product_count`
or `Corpus.all_products` — the two systems holding the answer. Nothing failed;
two systems were simply not consulted, and no test or gate could see a
subsystem that was never called. Only a trace of the *expected* call chain can.

Run:
    .venv\\Scripts\\python scripts\\trace_use_cases.py
    .venv\\Scripts\\python scripts\\trace_use_cases.py --live http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# (uc, trigger, subsystems that MUST be consulted, a substring the answer must contain)
CASES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("UC-01 catalogue question", "tell me about the X-Ray Cassette",
     ("classify", "resolve", "retrieve", "answer"), "X-Ray Cassette"),
    ("UC-05 price", "what is the price of the ultrasound gel?",
     ("classify", "resolve", "answer"), "60"),
    ("UC-05 MOQ", "what is the minimum order quantity for the ultrasound gel?",
     ("classify", "resolve", "answer"), "10"),
    ("UC-05 delivery", "what is the delivery time for the ultrasound gel?",
     ("classify", "resolve", "answer"), "7"),
    ("UC-06 compare", "compare the Ultrasound Gel and the X-Ray Cassette",
     ("classify", "resolve", "answer"), "Ultrasound Gel"),
    ("UC-07 request human", "do you sell MRI machines?",
     ("classify", "retrieve", "answer"), "get in touch"),
    ("UC-08 contact details", "call me on 98200 11122",
     ("classify", "resolve", "leads.capture", "answer"), "passed your details"),
    ("UC-11 clinical", "is this safe for a pacemaker patient?",
     ("classify", "answer"), "medical advice"),
    ("Listing", "Show me the catalogue",
     ("classify", "product_count", "answer"), "24 products"),
    ("Company", "where is your office?",
     ("classify", "answer"), "Mumbai"),
    ("Help", "what can you do",
     ("classify", "answer"), "specification"),
    ("Social", "I am Dr Pradeep",
     ("classify", "answer"), "Dr Pradeep"),
    ("Off-topic", "who won the world cup?",
     ("classify", "answer"), "don't have that"),
)


def trace_all(live_url: str | None) -> int:
    from anita import orchestrator as orch
    from anita.assembly import assemble, deployed_environment

    built = assemble(require_preflight=False, env=deployed_environment())
    chat = built.chat
    if chat.retriever is not None:
        try:
            chat.retriever.build_index()
        except Exception:  # noqa: BLE001
            pass

    touched: set[str] = set()
    original_classify = orch.classify

    def traced_classify(text):
        touched.add("classify")
        return original_classify(text)
    orch.classify = traced_classify

    original_resolve = chat.orchestrator.registry.resolve

    def traced_resolve(envelope, **kw):
        touched.add("resolve")
        return original_resolve(envelope, **kw)
    chat.orchestrator.registry.resolve = traced_resolve

    if chat.retriever is not None:
        original_retrieve = chat.retriever.retrieve

        def traced_retrieve(q, **kw):
            touched.add("retrieve")
            return original_retrieve(q, **kw)
        chat.retriever.retrieve = traced_retrieve

    if chat.answers is not None:
        original_count = chat.answers.product_count

        def traced_count():
            touched.add("product_count")
            return original_count()
        chat.answers.product_count = traced_count

        original_commodity = chat.answers.commercial

        def traced_commercial(kind, product):
            touched.add("commercial")
            return original_commodity(kind, product)
        chat.answers.commercial = traced_commercial

        original_compare = chat.answers.compare

        def traced_compare(left, right):
            touched.add("compare")
            return original_compare(left, right)
        chat.answers.compare = traced_compare

    if chat.leads is not None:
        original_capture = chat.leads.capture

        def traced_capture(*a, **kw):
            touched.add("leads.capture")
            return original_capture(*a, **kw)
        chat.leads.capture = traced_capture

    original_answer = chat._answer

    def traced_answer(intent, message, conversation):
        touched.add("answer")
        return original_answer(intent, message, conversation)
    chat._answer = traced_answer

    print("=" * 104)
    print("USE CASE TRACE — which subsystems each trigger actually reached")
    print("=" * 104)
    print(f"{'USE CASE':24} {'INTENT':11} {'ANSWER':6} {'MISSING SUBSYSTEM':34} REACHED")
    print("-" * 104)

    gaps: list[tuple[str, list[str]]] = []

    for uc, trigger, expected, want in CASES:
        touched.clear()
        try:
            body = chat.post_message(message=trigger, session_id=f"uc-{uc[:9]}").body
            text = body.get("text") or ""
            intent = body.get("intent") or "?"
        except Exception as exc:  # noqa: BLE001
            print(f"{uc:24} EXCEPTION {type(exc).__name__}: {exc}")
            gaps.append((uc, ["exception"]))
            continue

        missing = [s for s in expected if s not in touched]
        answered = "ok" if want.lower() in text.lower() else "WRONG"
        reached = ",".join(sorted(touched))

        print(f"{uc:24} {intent:11} {answered:6} "
              f"{(','.join(missing) if missing else '—'):34} {reached[:38]}")
        if missing or answered == "WRONG":
            gaps.append((uc, missing or ["answer did not contain " + repr(want)]))

    print("-" * 104)
    if gaps:
        print(f"\n{len(gaps)} USE CASE(S) WITH A GAP:\n")
        for uc, why in gaps:
            print(f"  {uc}")
            print(f"     {', '.join(why)}")
    else:
        print("\nEvery use case reached the subsystems it is supposed to.")

    if live_url:
        print("\n" + "=" * 104)
        print("LIVE SERVICE — same triggers, checking the running process agrees")
        print("=" * 104)
        stale = 0
        for uc, trigger, _, _ in CASES:
            try:
                request = urllib.request.Request(
                    live_url.rstrip("/") + "/api/chat/message",
                    data=json.dumps({"message": trigger, "session_id": "uc-live"}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    served = json.loads(response.read().decode()).get("text", "")
            except Exception as exc:  # noqa: BLE001
                print(f"  {uc:24} UNREACHABLE — {type(exc).__name__}")
                stale += 1
                continue
            print(f"  {uc:24} {served[:64]}")
        if stale == 0:
            print("\n  All triggers served. A mismatch with the traced answers above")
            print("  means the running process is stale — restart it.")
    print("=" * 104)
    return 1 if gaps else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", default=None, help="also drive a running API")
    args = parser.parse_args()
    return trace_all(args.live)


if __name__ == "__main__":
    raise SystemExit(main())
