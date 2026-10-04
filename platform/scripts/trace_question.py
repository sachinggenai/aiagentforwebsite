"""Trace one buyer question through every subsystem that handles it.

Answers five questions for a single turn, against the real assembly:

  1. what the user sees
  2. which internal systems were touched, in order
  3. what was asked of each one
  4. what each was *supposed* to return
  5. what it actually returned

The trace is taken by wrapping the live methods on the assembled instance, so
the call chain is observed rather than inferred from reading the code. Pair it
with a request to the running API to confirm the user-visible answer matches.

Run:
    .venv\\Scripts\\python scripts\\trace_question.py "Show me the catalogue"
    .venv\\Scripts\\python scripts/trace_question.py "Show me the catalogue" --live http://127.0.0.1:8100
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

events: list[dict] = []


def note(hop: str, subsystem: str, asked: str, got: str,
         intended: str = "", verdict: str = "") -> None:
    events.append({
        "hop": hop, "subsystem": subsystem, "asked": asked,
        "got": got, "intended": intended, "verdict": verdict,
    })


def clip(value, n: int = 96) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def trace(question: str, live_url: str | None) -> int:
    from anita.assembly import assemble, deployed_environment

    print("=" * 100)
    print(f"TRACE: {question!r}")
    print("=" * 100)

    built = assemble(require_preflight=False, env=deployed_environment())
    chat = built.chat

    # -- hop 1: the surface -------------------------------------------------
    original_post = chat.post_message

    def traced_post(*, message, session_id):
        note("1", "ChatSurface.post_message",
             f"message={clip(message, 40)!r} session_id={session_id!r}",
             "…", "validate, rate-limit, route, answer",
             "entry point")
        return original_post(message=message, session_id=session_id)

    chat.post_message = traced_post

    # -- hop 2: rate limiter ------------------------------------------------
    if chat.rate_limit is not None:
        original_check = chat.rate_limit.check

        def traced_check(key):
            try:
                original_check(key)
                note("2", "RateLimiter.check", f"key={key!r}", "allowed",
                     "admit within window", "ok")
            except Exception as exc:  # noqa: BLE001
                note("2", "RateLimiter.check", f"key={key!r}", f"refused: {exc}",
                     "admit within window", "REFUSED")
                raise
        chat.rate_limit.check = traced_check

    # -- hop 3: intent classification ---------------------------------------
    from anita import orchestrator as orch
    original_classify = orch.classify

    def traced_classify(text):
        intent = original_classify(text)
        note("3", "orchestrator.classify", f"text={clip(text, 40)!r}",
             intent.value, "→ a route", intent.value)
        return intent
    orch.classify = traced_classify

    # -- hop 4: conversation registry ---------------------------------------
    original_resolve = chat.orchestrator.registry.resolve

    def traced_resolve(envelope, **kw):
        conversation = original_resolve(envelope, **kw)
        note("4", "ConversationRegistry.resolve",
             f"channel={envelope.channel.value} text={clip(envelope.text, 32)!r}",
             f"conversation {conversation.conversation_id[:8]} "
             f"({len(conversation.turns)} prior turns)",
             "group this turn with the session's conversation", "ok")
        return conversation
    chat.orchestrator.registry.resolve = traced_resolve

    # -- hop 5: whatever the answer path reaches ----------------------------
    if chat.answers is not None:
        answers = chat.answers
        original_count = answers.product_count

        def traced_count():
            answer = original_count()
            note("5", "AnitaAnswers.product_count", "(no arguments)",
                 clip(answer.text, 70),
                 "a count from the products table",
                 "grounded" if answer.found else "NOT FOUND")
            return answer
        answers.product_count = traced_count

        original_all = answers.corpus.all_products

        def traced_all():
            products = original_all()
            note("5b", "Corpus.all_products → ams.sqlite",
                 "SELECT * FROM products",
                 f"{len(products)} rows",
                 "every catalogue product", "ok" if products else "EMPTY")
            return products
        answers.corpus.all_products = traced_all

    if chat.retriever is not None:
        original_retrieve = chat.retriever.retrieve

        def traced_retrieve(query, **kw):
            result = original_retrieve(query, **kw)
            note("5c", "Retriever.retrieve → GroundingGate",
                 f"query={clip(query, 40)!r}",
                 f"{len(result.chunks)} chunks, gate={result.decision.reason}",
                 "grounded context or an explicit refusal",
                 "ok" if result.ok else "declined")
            return result
        chat.retriever.retrieve = traced_retrieve

    # -- hop 6: the answer branch taken -------------------------------------
    original_answer = chat._answer

    def traced_answer(intent, message, conversation):
        reply = original_answer(intent, message, conversation)
        note("6", "ChatSurface._answer",
             f"intent={intent.value}",
             f"branch taken, grounded={reply.grounded}, "
             f"unavailable={reply.unavailable}",
             "the branch matching this intent", "ok")
        return reply
    chat._answer = traced_answer

    # -- run ----------------------------------------------------------------
    body = chat.post_message(message=question, session_id="trace-1").body

    print("\n1. WHAT THE USER SEES\n")
    print(f"   {body.get('text')}")
    print(f"\n   intent={body.get('intent')}  grounded={body.get('grounded')}  "
          f"source={body.get('source_url') or '—'}")

    print("\n2. SUBSYSTEMS TOUCHED, IN ORDER\n")
    for event in events:
        print(f"   [{event['hop']:>3}] {event['subsystem']}")
        print(f"         asked    : {event['asked']}")
        print(f"         intended : {event['intended']}")
        print(f"         actual   : {event['got']}")

    if live_url:
        print("\n3. THE RUNNING SERVICE, SAME QUESTION\n")
        try:
            request = urllib.request.Request(
                live_url.rstrip("/") + "/api/chat/message",
                data=json.dumps({"message": question, "session_id": "trace-live"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                served = json.loads(response.read().decode())
            same = served.get("text") == body.get("text")
            print(f"   {clip(served.get('text'), 200)}")
            print(f"\n   matches the traced answer: {same}")
        except Exception as exc:  # noqa: BLE001
            print(f"   could not reach {live_url}: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 100)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="Show me the catalogue")
    parser.add_argument("--live", default=None,
                        help="also ask a running API and compare the answers")
    args = parser.parse_args()
    return trace(args.question, args.live)


if __name__ == "__main__":
    raise SystemExit(main())
