# Full-duplex WebRTC audio — an additive plan

> **Status: Phase 1 built and working. Phase 2 declined — see §5b.**
> **Phase 0 has been run — see §2a.** Media flows between a browser and this
> box, and the network position is better than §2 first claimed. The remote half needs one
> browser on a different network to finish.
> **Read §1 before costing this** — the runbook line being fixed describes two different
> things, and only one of them is WebRTC.
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

## 2. The network — and a correction Phase 0 forced

> **Revised after the spike. The original claim here was too strong.** It said the server had
> no inbound reachability and that a relay would therefore be mandatory. Phase 0 measured the
> candidate set instead of reasoning about it, and the machine **does** offer a public path.
> See §5a for the numbers. The reasoning below is kept, corrected.

The public ingress is a **Cloudflare quick tunnel** pointing at the API port — an HTTP/WebSocket
tunnel with a random hostname. Signalling crosses it fine (HTTPS POST). Media does not: WebRTC
sends media peer-to-peer over ICE, *not* through the tunnel.

So the question is not "can the tunnel carry media" (it cannot) but **"can a browser reach this
machine directly"** — which the spike answered empirically:

* The server gathers **`srflx` candidates** — its public IPv4 as seen by a STUN server, on an
  ephemeral NAT mapping. ICE hole-punching through that is exactly how ordinary WebRTC works on
  a home or office network, and it often succeeds.
* The machine also holds **global IPv6 addresses**, where no NAT traversal is needed at all if
  the peer has IPv6.

Neither is a guarantee. Both depend on the *client's* network, the NAT's type, and whether the
mapping survives. So the honest position is: **direct connectivity is plausible and unproven**,
and a relay is the fallback rather than the prerequisite.

If it turns out a relay is needed for the client's network, the options are:

| | What it adds | Notes |
|---|---|---|
| **TURN server** (coturn) | A new public host with UDP/TCP 3478 open, credentials, and its own certificate for TURN-over-TLS | The standard answer. Needs a host with a real public IP — the tunnel cannot provide one |
| **A hosted SFU** (LiveKit, Cloudflare Realtime, Daily) | A vendor, an API key, and media leaving your infrastructure | Least operational work, most dependency, per-minute cost |
| **Direct/LAN only** | Nothing | Works when the browser and the server are on the same network — which covers a client sitting in the room, and *only* that |

**The UDP claim could not be verified by search in this session** — the tool was erroring —
which is why Phase 0 measures it rather than assuming it.

## 2a. Phase 0 — what the spike found

Built and run: `aiortc` server + a Chromium client with a fake capture device, in a throwaway
venv (`%TEMP%\webrtc-spike`). Nothing was wired into ANITA.

| Question | Result |
|---|---|
| Does `aiortc` work on this box's Python? | **Yes** — 1.15.0 installs and imports on **Python 3.14.7**. The dependency risk is retired |
| Does media actually flow? | **Yes** — the server received **313 / 315 audio frames** in two runs, with `iceConnectionState: connected` |
| What does the server offer a peer? | `host` on `192.168.1.5` (LAN), `host` on `172.22.160.1` (a virtual adapter), `host` on two **global IPv6** addresses (`2401:4900:…`), and **`srflx`** on `106.215.182.198:<ephemeral>` |
| Does a remote peer work? | **Still unproven** — needs a browser on a different network. See below |

Two things this settles: the transport is buildable here, and the box is **not** unreachable —
it produces exactly the candidates ordinary WebRTC hole-punching relies on.

Note the `srflx` candidate appeared without a STUN server being configured, because `aiortc`'s
default ICE configuration already carries one. A real deployment should configure it
deliberately rather than inherit it.

**The remote half** needs a peer that is not this machine. The spike has been exposed through a
quick tunnel for exactly that: opening its URL **from a phone on mobile data** (not office
wi-fi) answers the question in about a minute. If ICE reports `connected` there, no relay is
needed for the demo and a large part of this plan's cost disappears; if it reports `failed`,
that is the case for TURN, measured rather than argued.

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

## 5b. Phase 2 — declined, and the reason is worth keeping

Decided 2026-10-16: **not doing it.** What settled it is not what this plan originally said,
and the correction matters more than the decision.

**Phase 2 cannot be additive, and the echo gate is why.**

* The turn model *is* the engine: `feed_audio()` accumulates until the VAD declares
  end-of-utterance and returns `True`, then `process_utterance()` runs (`voice.py:1161`,
  `:1218`). There is no seam to hang a second mode behind.
* `admit_inbound_frame()` returns `FRAME_ECHO` to reject the assistant's own playback arriving
  back through the microphone (`voice.py:945-1018`). **That gate works only because the two
  sides never speak at once.** Simultaneous audio breaks its central assumption — and it is not
  incidental code: it was the fix for defect §8.30, where the barge-in window was dead on real
  calls.

So Phase 2 edits `voice.py`: the file the "nothing breaks" constraint protects, and the one
this repo guards with a vendored definition manifest. Weeks of work against an explicit
constraint, for a difference barge-in already covers in a short demo.

**What is true as a result:** the browser-audio transport *is* WebRTC and may be described as
such. It is **not** full duplex, and the runbook now says so in two places rather than one.

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
