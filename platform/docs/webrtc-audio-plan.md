# Full-duplex WebRTC audio — an additive plan

> **Status:** plan, nothing built. **Read §2 before costing this** — the runbook line being
> fixed describes two different things, and only one of them is WebRTC.
> **Constraint from the request:** additive. The running build keeps working, the existing
> browser-mic path stays the default, and nothing already demonstrated changes behaviour.

## 1. Why this is not one feature

The line in the runbook —

> *Full-duplex WebRTC audio — cannot be demoed. The browser audio is a WebSocket carrying
> audio frames — turn-based with interruption, not simultaneous. Do not describe it as WebRTC.*

— is true, but it compresses two independent gaps into one row. They cost very different
amounts, and separating them is most of the planning:

| | What it actually is | Cost |
|---|---|---|
| **"Not WebRTC"** | A **transport** gap. The socket is a WebSocket carrying base64 µ-law, not SRTP/Opus over UDP | Moderate, and genuinely additive |
| **"Not simultaneous"** | A **conversation-model** gap. The engine is turn-based: VAD closes an utterance, the answer plays, and the caller may interrupt | Large, and *not* additive — it changes `voice.py` |

**A correction worth carrying into the demo.** The *socket* is already full duplex — audio
flows both ways continuously for the whole call. What is turn-based is the **engine**, which
waits for the end of an utterance before it answers. So "the browser audio is not
simultaneous" does not mean the channel is half-duplex; it means the assistant takes turns.
Whoever explains this to a client should get that right, and the runbook row should be
reworded either way.

That matters because **Phase 1 alone makes the WebRTC claim true**, without touching the
engine at all — and Phase 2 is where the risk is.

## 2. The blocker is the network, not the code

This is the part to settle before writing any Python.

The public ingress is a **Cloudflare quick tunnel** pointing at the API port. It is an
HTTP/WebSocket tunnel with a random hostname. Two consequences for WebRTC media:

1. **The server has no inbound reachability.** It sits behind the tunnel; there is no public
   address a browser could send UDP media to. Direct host/STUN candidates cannot work, so
   ICE would require a **relay**.
2. **A quick tunnel does not relay UDP.** TURN is therefore not something the existing
   ingress can stand in for; it is a *separate public endpoint*.

So real WebRTC in the demo topology needs one of:

| | What it adds | Notes |
|---|---|---|
| **TURN server** (coturn) | A new public host with UDP/TCP 3478 open, credentials, and its own certificate for TURN-over-TLS | The standard answer. Needs a host with a real public IP — the tunnel cannot provide one |
| **A hosted SFU** (LiveKit, Cloudflare Realtime, Daily) | A vendor, an API key, and media leaving your infrastructure | Least operational work, most dependency, per-minute cost |
| **Direct/LAN only** | Nothing | Works when the browser and the server are on the same network — which covers a client sitting in the room, and *only* that |

**I could not verify the UDP claim in this session** — the search tool was returning errors —
and it is load-bearing enough that Phase 0 exists to prove it rather than assume it. The
inbound-reachability argument in (1) does not depend on the UDP question and holds regardless.

## 3. What the engine requires, and why that helps

Read from the code, and it is the reason an additive design is possible at all. A transport
does not need to understand the engine — it needs to satisfy a **four-call seam**:

```
session.admit_inbound_frame(ulaw_chunk) -> LISTENING | INTERRUPTION | ECHO | MUTED
session.feed_audio(frame)
session.process_utterance(...)          -> chunks, transcript, end_call
session.mark_speaking() / note_playback() / request_interruption()
```

`BrowserVoiceSession` calls exactly these (`browser_voice.py:312-366`); so does the Twilio
relay, in its own way. **A third transport can call the same four things and the engine never
learns it exists.**

Two facts that make the fit concrete:

* The audio contract is **µ-law, 8 kHz, 20 ms frames** (`voice.py:454`). WebRTC delivers
  **Opus, 48 kHz**. So a WebRTC transport has to transcode — and there is already a
  precedent to copy: `pcm16_to_ulaw()` in `browser_voice.py` does exactly this kind of
  adaptation today, including a deliberate anti-aliasing filter rather than decimating by
  dropping samples.
* The same `AdmissionController`, `SessionTokens`, conversation identity, sentiment and
  escalation all sit *above* the seam, so a WebRTC call inherits every one of them for free.

## 4. The additive design

```
                    ┌─────────────────── unchanged ───────────────────┐
  browser ──WS──────► BrowserVoiceSession ──┐                        │
  (today)           └───────────────────────┤                        │
                                            ├──► VoiceCallSession ────┤──► answer path
  browser ──WebRTC──► WebRTCVoiceSession ───┘    (voice.py, untouched)│    (unchanged)
  (new)             └── new module ─────────┘                        │
                    └────────────────────────────────────────────────┘
```

**New, and only new:**

| Piece | What it is |
|---|---|
| `app/anita/webrtc_voice.py` | `RTCPeerConnection` handling: offer/answer, ICE, Opus decode → µ-law 20 ms frames → the four-call seam → Opus encode back |
| `POST /api/voice/webrtc/offer` | Signalling. Takes an SDP offer + a session ticket, returns an SDP answer. Authenticated with the **existing** `SessionTokens` ticket — the same one `POST /api/voice/ticket` already mints |
| `app/static/webrtc.html` (or a toggle on `chat.html`) | A second client page. The existing mic button keeps pointing at the WebSocket |
| A config flag | `ANITA_WEBRTC_ENABLED`, default **off** |

**Untouched:** `voice.py`, `browser_voice.py`, `media_stream.py`, the WebSocket route, the
Twilio paths, the answer engine, and every existing test. The new transport is a *sibling*
of `BrowserVoiceSession`, not a modification of it.

### The three traps, all of which have bitten this repo before

1. **A new dependency must not reach the boot path.** `aiortc` pulls in `aioice`,
   `pylibsrtp` and `cryptography`. It must be imported lazily, inside the route, exactly as
   `twilio.rest` is today — otherwise an instance that never uses WebRTC fails to start
   without it. `requirements.txt` gains it under an optional heading, like `torch`.
2. **A new env key must be read from `app/anita/`.** `ANITA_WEBRTC_ENABLED` read only by a
   script or a page is classified `INERT` by the config audit, and `boot.py` **refuses the
   whole boot** on an inert key. This trap has already caught two changes in this repo.
3. **The existing paths must stay the default.** `ANITA_WEBRTC_ENABLED=false` ships, so a
   deployment that never opts in behaves *byte-identically* to today.

## 5. Phases

### Phase 0 — the spike (half a day, throwaway code)

**One question: does media flow between a browser and this box in the demo topology?**
A ten-line `aiortc` server answering an offer from a scratch HTML page, run twice — once
over the LAN, once with the browser on a different network through the tunnel. Nothing is
wired into ANITA; the code is deleted either way.

* **Passes over LAN, fails remote** → confirmed: TURN or an SFU is required. Decide §2 before
  writing phase 1.
* **Passes both** → the transport is buildable as planned.
* **Fails both** → the box's own audio path is the problem, not the network, and the estimate
  below is wrong.

This phase is not optional. Everything after it is costed on its answer.

### Phase 1 — the WebRTC transport, turn-based (3–5 days)

Same conversation semantics as today; only the wire changes. Deliverable: `/webrtc` works,
and *"the browser audio is WebRTC"* becomes a true sentence.

Includes: the transcoder (Opus ⇄ µ-law, with a test that a known waveform survives the round
trip), the signalling route behind the existing ticket, ICE configuration, and the close/error
paths mapped to the codes the current client already understands.

**Nothing in the existing build changes**, and the full suite must stay green untouched.

### Phase 2 — true full duplex (2–3 weeks, and the risky one)

This is the part that is *not* additive, so it needs saying plainly.

Continuous audio means reworking the engine's turn model: when does an utterance end, what
does the echo gate do while the assistant is mid-sentence, how do barge-in thresholds work
when there is no turn boundary, what happens to the 30-minute cap and transcript truncation.
`voice.py` carries all of it — and it is the one file in this repo guarded by a vendored
definition manifest (`test_mod05_voice_migration.py`), so changes there are visible by design.

Two honest points before committing to it:

* **Barge-in already delivers most of what a demo shows.** The product already stops mid
  sentence when spoken over. The difference between that and "full duplex" is perceptible in
  a lab test and much less so in a five-minute demo.
* **It risks the thing that currently works.** Voice is already the surface with the most
  moving parts and the most environment sensitivity (the VRAM ladder, the first-call warm-up,
  the barge-in thresholds that have never been confirmed on a real handset).

**Recommendation: do Phase 0, then Phase 1. Treat Phase 2 as a separate decision made after
Phase 1 is running**, because Phase 1 is what removes the disqualifying sentence from the
runbook, and Phase 2 is what could take voice down.

## 6. What it costs beyond code

| | |
|---|---|
| **CPU** | Opus encode/decode per call, plus resampling — modest, but on a box whose capacity ladder already sheds voice under GPU pressure |
| **Bandwidth** | Higher than µ-law: Opus wideband is roughly 24–40 kbps against 64 kbps G.711, but with WebRTC overhead and a relay it is a real increase per call |
| **A relay, if remote** | Either a TURN host to run and secure, or a vendor with a bill |
| **A new failure mode** | ICE that fails on a restrictive client network. Today a WebSocket either connects or does not; WebRTC can connect and then deliver nothing |

## 7. How to know it works, and that nothing broke

* **Unchanged proves itself:** the full suite stays green, and `tests/e2e/run_voice.sh`
  (the existing browser-mic Playwright scenarios) keeps passing against the WebSocket path
  with WebRTC enabled but unused. If either moves, the change was not additive.
* **New:** a WebRTC-specific scenario — connect, speak, get a transcript and audio back,
  interrupt, hang up — run desktop and mobile like the existing voice suite.
* **The claim itself:** once Phase 1 lands, `platform/docs/demo-runbook.html` §"What you
  cannot demo" loses this row, and the browser-audio act can say WebRTC without a caveat.
  If Phase 2 does not happen, the act still must not say *"full duplex"*.

## 8. Decision needed

1. **Is the demo remote, or in the room?** In the room, Phase 0 likely passes on the LAN and
   no relay is needed — which removes most of §2 and a large part of the cost.
2. **If remote: TURN host, or an SFU vendor?** Nothing can be costed until this is answered.
3. **Is Phase 2 actually wanted?** Phase 1 makes the sentence true; Phase 2 makes the audio
   simultaneous. They are separable, and only one of them risks the working voice path.
