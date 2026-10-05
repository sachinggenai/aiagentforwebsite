"""US-001 acceptance — drive the carrier path without a carrier.

Twilio's side of a call is three things: a signed webhook POST, a WebSocket
that carries µ-law frames, and a status callback. All three can be driven from
this machine, over the same public tunnel Twilio would use — which is what this
script does, so `T-01`…`T-17` are not gated on a phone in someone's hand.

    .venv\\Scripts\\python scripts\\carrier_acceptance.py            # run everything runnable
    .venv\\Scripts\\python scripts\\carrier_acceptance.py --check    # preconditions only
    .venv\\Scripts\\python scripts\\carrier_acceptance.py --no-audio # skip the media round trip

It is deliberately the *client* a carrier would be: it signs with the real
auth token, downloads the TwiML, reads the `call_id`/`token` parameters out of
it, and opens the media stream with them — so a stream that does not carry the
webhook's token is refused here exactly as it would be for an attacker.

What it cannot prove is printed at the end as a human checklist: a handset's
audio, a real CallSid in the table, and the timing budgets a real network adds.

Exit codes: 0 every check that ran passed · 1 something failed · 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import urllib.parse
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))
sys.path.insert(0, str(PLATFORM.parent))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"
_results: list[tuple[str, str, str]] = []


def report(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {PASS: "  [PASS]", FAIL: "  [FAIL]", SKIP: "  [SKIP]", INFO: "  [INFO]"}[status]
    print(f"{mark} {name}" + (f"  — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# The Twilio side of the exchange
# ---------------------------------------------------------------------------
class Carrier:
    """Signs, posts and dials the way Twilio does."""

    def __init__(self, env: dict) -> None:
        self.env = env
        self.host = (env.get("TUNNEL_HOST") or "").strip()
        self.token = (env.get("TWILIO_AUTH_TOKEN") or "").strip()
        self.number = (env.get("TWILIO_PHONE_NUMBER") or "").strip()
        self.base = f"https://{self.host}"
        self._call_seq = 0

    def next_call_id(self, label: str) -> str:
        """A CallSid-shaped id: 34 chars, `CA` + hex, unique per run.

        The shape matters — the channel tables and the logs treat it as an
        identifier, and a short id would hide a truncation bug.
        """
        self._call_seq += 1
        stem = f"{int(time.time() * 1000):x}{self._call_seq:04x}"
        return ("CA" + stem.rjust(32, "0"))[:34]

    def post_voice(self, params: dict, *, signed: bool = True):
        """POST /twilio/voice exactly as Twilio would. Returns (status, body)."""
        import httpx

        from anita.channels import signed_payload

        url = f"{self.base}/twilio/voice"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if signed:
            headers["X-Twilio-Signature"] = signed_payload(self.token, url, params)
        response = httpx.post(
            url, content=urllib.parse.urlencode(params).encode(),
            headers=headers, timeout=30.0,
        )
        return response.status_code, response.text

    def stream_parameters(self, twiml: str) -> dict:
        """The `<Parameter>` values the webhook handed the stream."""
        import xml.etree.ElementTree as ET

        stream = ET.fromstring(twiml).find("./Connect/Stream")
        if stream is None:
            return {}
        return {
            p.get("name"): p.get("value")
            for p in stream.findall("Parameter")
        }


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def check_preconditions(carrier: Carrier) -> bool:
    print("\n1. Preconditions")
    ok = True
    for key, value in (
        ("TWILIO_AUTH_TOKEN", carrier.token),
        ("TWILIO_PHONE_NUMBER", carrier.number),
        ("TUNNEL_HOST", carrier.host),
    ):
        present = bool(value)
        report(PASS if present else FAIL, f"{key} is set", "" if present else "missing")
        ok &= present
    if not carrier.host:
        return False

    import httpx

    try:
        health = httpx.get(f"{carrier.base}/health", timeout=20.0)
        report(PASS if health.status_code == 200 else FAIL,
               "the instance answers over the tunnel",
               f"GET {carrier.base}/health -> {health.status_code}")
        ok &= health.status_code == 200
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "the instance answers over the tunnel", f"{type(exc).__name__}: {exc}")
        ok = False
    return ok


def check_signature(carrier: Carrier) -> dict:
    """`T-16` — the signature is computed for the *public* URL, and the TwiML
    carries the stream parameters the relay will demand."""
    print("\n2. T-16 — signature, attribution and TwiML")
    call_id = carrier.next_call_id("acc")
    params = {"CallSid": call_id, "From": "+15550000001", "To": carrier.number}
    status, body = carrier.post_voice(params)

    report(PASS if status == 200 else FAIL, "a signed webhook is admitted",
           f"HTTP {status}")
    has_stream = "<Stream" in body
    report(PASS if has_stream else FAIL, "TwiML hands out the media stream",
           body.strip().splitlines()[-2].strip() if has_stream else body[:120])
    fields = carrier.stream_parameters(body)
    report(PASS if fields.get("call_id") == call_id else FAIL,
           "the stream is told which call it is", fields.get("call_id", ""))
    report(PASS if fields.get("token") else FAIL,
           "the stream carries an admission token",
           "minted" if fields.get("token") else "absent — §8.6 gate would refuse every call")

    unsigned_status, unsigned_body = carrier.post_voice(params, signed=False)
    report(PASS if unsigned_status == 403 and not unsigned_body else FAIL,
           "T-05 — an unsigned webhook is refused",
           f"HTTP {unsigned_status}, {len(unsigned_body)} bytes")

    withheld = {"CallSid": carrier.next_call_id("acc"), "To": carrier.number}
    status, body = carrier.post_voice(withheld)
    report(PASS if status == 200 and "<Stream" in body else FAIL,
           "T-06 — a withheld number still gets a stream", f"HTTP {status}")
    return {"call_id": call_id, "params": fields}


class MediaCall:
    """One simulated call's media stream."""

    def __init__(self, relay_url: str, call_id: str, token: str, caller: str) -> None:
        self.url = relay_url
        self.call_id = call_id
        self.token = token
        self.caller = caller
        self.media: list[bytes] = []
        self.first_audio_at: float | None = None
        self.sent_at: float | None = None
        self.closed_code: int | None = None

    async def run(self, frames: list[bytes], *, hold: float = 12.0,
                  stop: bool = True) -> None:
        import websockets

        try:
            async with websockets.connect(self.url, open_timeout=20) as ws:
                await ws.send(json.dumps(self._start()))
                for frame in frames:
                    await ws.send(json.dumps(self._media(frame)))
                    await asyncio.sleep(0.02)   # real time: 20 ms per frame
                self.sent_at = time.monotonic()
                deadline = time.monotonic() + hold
                while time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    message = json.loads(raw)
                    if message.get("event") == "media":
                        if self.first_audio_at is None:
                            self.first_audio_at = time.monotonic()
                        self.media.append(base64.b64decode(message["media"]["payload"]))
                    if message.get("event") == "mark" and self.media and stop:
                        break
                if stop:
                    await ws.send(json.dumps({"event": "stop", "stop": {}}))
        except Exception as exc:  # noqa: BLE001
            self.closed_code = getattr(exc, "code", None)
            raise

    def _start(self) -> dict:
        return {
            "event": "start",
            "start": {
                "streamSid": f"MZ{self.call_id[-12:]}",
                "callSid": self.call_id,
                "from": self.caller,
                "customParameters": {"call_id": self.call_id, "token": self.token},
            },
        }

    @staticmethod
    def _media(frame: bytes) -> dict:
        return {
            "event": "media",
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        }

    def first_audio_ms(self) -> float | None:
        if self.first_audio_at is None or self.sent_at is None:
            return None
        return (self.first_audio_at - self.sent_at) * 1000


def caller_audio(question: str) -> list[bytes] | None:
    """Synthesise the caller's question into 8 kHz µ-law frames.

    The carrier-free round trip needs *speech*, not a tone: the point of `T-01`
    is that the real STT leg hears a real question. Kokoro speaks it and the
    engine's own converter puts it on the wire, so the audio path under test is
    the same one a carrier feeds.
    """
    try:
        import numpy as np
        from kokoro_onnx import Kokoro
        from scipy.signal import resample_poly

        from anita.voice import pcm_to_ulaw
        from anita.voice_settings import settings
    except Exception as exc:  # noqa: BLE001
        report(SKIP, "caller audio", f"TTS unavailable ({type(exc).__name__})")
        return None

    cache = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
    model_path, voices_path = cache / "kokoro-v1.0.onnx", cache / "voices-v1.0.bin"
    if not (model_path.exists() and voices_path.exists()):
        report(SKIP, "caller audio", f"Kokoro model not cached at {cache}")
        return None

    try:
        samples, rate = Kokoro(str(model_path), str(voices_path)).create(
            question, voice=settings.KOKORO_VOICE, speed=settings.KOKORO_SPEED
        )
    except Exception as exc:  # noqa: BLE001
        report(SKIP, "caller audio", f"{type(exc).__name__}: {str(exc)[:60]}")
        return None

    # 24 kHz float → 8 kHz int16 → 160-byte µ-law frames.
    resampled = resample_poly(np.asarray(samples, dtype="float32"), 8000, int(rate))
    pcm = (np.clip(resampled, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    ulaw = pcm_to_ulaw(pcm)
    return [ulaw[i:i + 160] for i in range(0, len(ulaw) - 159, 160)]


def check_media_round_trip(carrier: Carrier, parameters: dict) -> None:
    """`T-01` — a stream with the webhook's own parameters is answered."""
    print("\n3. T-01 — the media round trip (simulated carrier)")
    frames = caller_audio("How much is the ultrasound gel?")
    if frames is None:
        return

    call_id = carrier.next_call_id("acc")
    status, body = carrier.post_voice(
        {"CallSid": call_id, "From": "+15550000002", "To": carrier.number}
    )
    if status != 200 or "<Stream" not in body:
        report(FAIL, "the call was admitted for the round trip", f"HTTP {status}")
        return
    fields = carrier.stream_parameters(body)
    report(INFO, "caller audio", f"{len(frames)} frames ({len(frames) * 20} ms of speech)")

    call = MediaCall(
        f"wss://{carrier.host}/twilio/media-stream",
        fields.get("call_id", ""), fields.get("token", ""), "+15550000002",
    )
    try:
        asyncio.run(call.run(frames))
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "the stream carried the call", f"{type(exc).__name__}: {str(exc)[:80]}")
        return

    spoke = len(call.media) * 160
    report(PASS if call.media else FAIL, "the answer came back as audio",
           f"{len(call.media)} frames ({spoke * 8 // 1000} ms)")
    ms = call.first_audio_ms()
    if ms is not None:
        report(PASS if ms <= 5000 else INFO, "first audio after end of speech",
               f"{ms:.0f} ms (carrier-free; the budget is measured on a real call)")


def check_ceiling(carrier: Carrier) -> None:
    """`T-12`/`T-17` — the ceiling holds, and the refused call opens no stream."""
    print("\n4. T-12 — the admission ceiling")
    admitted: list[tuple[str, dict]] = []
    refused_at = 0
    for _ in range(6):
        call_id = carrier.next_call_id("acc")
        status, body = carrier.post_voice(
            {"CallSid": call_id, "From": "+15550000003", "To": carrier.number}
        )
        if status == 200 and "<Stream" in body:
            admitted.append((call_id, carrier.stream_parameters(body)))
            continue
        refused_at = len(admitted) + 1
        report(PASS if "<Stream" not in body else FAIL,
               "the refused call is given busy TwiML and no stream",
               f"call {refused_at} refused")
        break
    report(INFO, "effective ceiling", f"{len(admitted)} concurrent call(s) on this host")
    if not refused_at:
        report(INFO, "ceiling not reached", "6 calls admitted — raise the probe or check the cap")

    # Release every slot this check took, through the carrier's own path.
    for call_id, fields in admitted:
        call = MediaCall(f"wss://{carrier.host}/twilio/media-stream",
                         fields.get("call_id", ""), fields.get("token", ""), "+15550000003")
        try:
            asyncio.run(call.run([], hold=1.0, stop=True))
        except Exception:  # noqa: BLE001 - teardown is best-effort by design
            pass

    call_id = carrier.next_call_id("acc")
    status, body = carrier.post_voice(
        {"CallSid": call_id, "From": "+15550000004", "To": carrier.number}
    )
    report(PASS if status == 200 and "<Stream" in body else FAIL,
           "T-13 — a dropped stream released its slot; the next call is admitted",
           f"HTTP {status}")
    fields = carrier.stream_parameters(body)
    release = MediaCall(f"wss://{carrier.host}/twilio/media-stream",
                        fields.get("call_id", ""), fields.get("token", ""), "+15550000004")
    try:
        asyncio.run(release.run([], hold=1.0, stop=True))
    except Exception:  # noqa: BLE001
        pass


def check_refusal(carrier: Carrier) -> None:
    """`T-05` continued — a forged stream cannot ride the live call's slot."""
    print("\n5. §8.6 — the socket refuses what the webhook never admitted")
    import websockets

    call_id = carrier.next_call_id("acc")
    status, body = carrier.post_voice(
        {"CallSid": call_id, "From": "+15550000005", "To": carrier.number}
    )
    if not (status == 200 and "<Stream" in body):
        report(SKIP, "forged stream refused", "could not admit a call to name")
        return
    fields = carrier.stream_parameters(body)

    async def forge() -> int | None:
        try:
            async with websockets.connect(f"wss://{carrier.host}/twilio/media-stream") as ws:
                await ws.send(json.dumps({
                    "event": "start",
                    "start": {
                        "streamSid": "MZforged", "callSid": fields.get("call_id", ""),
                        "from": "+15550000009",
                        # The real call's id, but no token — what an attacker has.
                        "customParameters": {"call_id": fields.get("call_id", "")},
                    },
                }))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5.0)
                except Exception:  # noqa: BLE001 - the close is the evidence
                    pass
                return getattr(ws, "close_code", None)
        except Exception as exc:  # noqa: BLE001
            return getattr(exc, "code", None)

    code = asyncio.run(forge())
    report(PASS if code == 1008 else FAIL,
           "a tokenless stream naming a live call is closed",
           f"close code {code}")

    release = MediaCall(f"wss://{carrier.host}/twilio/media-stream",
                        fields.get("call_id", ""), fields.get("token", ""), "+15550000005")
    try:
        asyncio.run(release.run([], hold=1.0, stop=True))
    except Exception:  # noqa: BLE001
        pass


HUMAN_CHECKLIST = """
What only a real handset can prove — place these calls to {number} with
`start_services.ps1` running and watch the instance terminal plus
`logs/anita/`:

  T-02  Barge-in — talk over the answer: the remaining clauses stop, `clear` is
        sent once, the transcript is marked truncated (US-017).
  T-03  "Can I speak to someone?" — a handoff row appears and the caller is
        told when to expect contact (US-022); the console Escalations panel
        shows it open.
  T-04  Gibberish or silence three times — the ladder ends in a callback offer
        and the call closes (AC-7).
  T-07  Give a name with no email — the lead is saved partially, flagged.
  T-08  Ask the same question twice — answered twice, one lead (phone dedupe).
  T-09  Leave the line silent — the 30-minute cap closes it and the transcript
        survives (TAC-6).
  T-10  Stop Ollama mid-call — the apology and callback offer are spoken;
        a price question still answers (US-015).
  T-14  Call back from the same number within 6 h — the conversation and lead
        resume (US-004).
  T-15  Hang up after an answer — the summary job runs from the stored
        transcript, `conversation.closed` fires once (US-006).

  And the two records only a carrier produces:
    * a real 34-character CallSid in `inbound_event` (every id in the tables
      today is simulated), and
    * `T-01`/`T-17` timing on a real network — p95 first-audio <= 1.2 s and a
      refusal <= 150 ms — read from the turn traces.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="preconditions only")
    parser.add_argument("--no-audio", action="store_true",
                        help="skip the media round trip (no TTS model needed)")
    args = parser.parse_args(argv)

    from anita.assembly import deployed_environment

    env = deployed_environment()
    carrier = Carrier(env)
    print("=" * 84)
    print("US-001 CARRIER ACCEPTANCE — the Twilio side, driven without Twilio")
    print("=" * 84)

    if not check_preconditions(carrier):
        print("\ncould not run: fix the preconditions above (see docs/twilio-voice-setup.md)")
        return 2
    if args.check:
        return 0

    parameters = check_signature(carrier)
    if not args.no_audio:
        check_media_round_trip(carrier, parameters)
    check_ceiling(carrier)
    check_refusal(carrier)

    print("\n" + "=" * 84)
    failed = [name for status, name, _ in _results if status == FAIL]
    skipped = [name for status, name, _ in _results if status == SKIP]
    ran = sum(1 for status, _, _ in _results if status in (PASS, FAIL))
    print(f"  {ran - len(failed)} passed · {len(failed)} failed · {len(skipped)} skipped")
    if failed:
        print("  failed: " + "; ".join(failed))
    print(HUMAN_CHECKLIST.format(number=carrier.number or "<the ANITA number>"))
    print("=" * 84)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
