# Twilio voice setup — getting a real call answered

> **Audience:** operator. **Prerequisite:** US-001's transport is built (webhook, media-stream relay, channel tables).
> **Outcome:** dialling the ANITA number reaches this instance and is answered.

The number is **+12792365960** (`TWILIO_PHONE_NUMBER`). It must match exactly — attribution
compares the number Twilio says was called against the endpoint registered at boot, and a mismatch
is refused with a 403 even when the signature is valid.

---

## The one thing that bites

**`TUNNEL_HOST` must be set before the instance starts, and the instance must be restarted
whenever the tunnel hostname changes.**

Twilio signs the URL it called. A Cloudflare tunnel rewrites the `Host` header in transit, so
validating against the delivered header would fail every webhook closed and the product would look
dead. The instance therefore validates against a *configured* public URL, read **once at boot** —
so changing it without restarting has no effect, and a stale value rejects everything.

That matters more than usual because **quick tunnels mint a new hostname on every restart**. A
fixed ingress (a named tunnel or a domain) removes the whole class of problem; until then, expect
to repeat steps 2–5 each time you restart the tunnel.

---

## Step 1 — Confirm the instance boots

```powershell
cd D:\project\anitamedicalsystems\platform
.venv\Scripts\python.exe scripts\serve.py --check
```

Expect **one** degradation, not two:

```
degraded :
  - TUNNEL_HOST unset — Twilio has no address to call back...
```

If you also see `TWILIO_AUTH_TOKEN unset`, stop: `.env` is not being read, and every webhook will
403 on the signature check. That was fixed in `8402833` — confirm `.env` sits at
`platform/.env` (not the repo root).

---

## Step 2 — Start the tunnel

`cloudflared` is installed at `C:\Program Files (x86)\cloudflared\cloudflared.exe`.

```powershell
# in its own terminal; leave it running
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8100
```

The port is `ANITA_API_PORT` (8100). If you changed it with `--port`, use the same value here —
a tunnel pointing at the wrong port returns 502 and Twilio reports the call as failed.

---

## Step 3 — Capture the hostname

In the cloudflared output, find the line that looks like:

```
+--------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at:                        |
|  https://random-words-1234.trycloudflare.com                             |
+--------------------------------------------------------------------------+
```

Copy the hostname **without** the `https://` — you want `random-words-1234.trycloudflare.com`.

---

## Step 4 — Set it and restart

In `platform/.env`:

```
TUNNEL_HOST=random-words-1234.trycloudflare.com

# Optional: the number spoken in the busy message when capacity is full.
# Leave blank and only the busy message is played.
ANITA_CALLBACK_NUMBER=+12792365960
```

Then restart the instance:

```powershell
.venv\Scripts\python.exe scripts\serve.py
```

`--check` should now report **no** degradations. The route table reads 9 routes plus the three
Twilio paths, which are registered separately because they take form bodies and answer in TwiML
rather than JSON.

---

## Step 5 — Verify locally, before involving Twilio

This is the step worth doing. You can prove the whole webhook works without placing a call, by
computing the signature yourself and posting it:

```powershell
cd D:\project\anitamedicalsystems\platform
.venv\Scripts\python.exe -c @"
import sys, json, urllib.request, urllib.parse
sys.path.insert(0, 'app')
from anita.channels import signed_payload
from anita.config import load_settings

s = load_settings(None)
host = s.env['TUNNEL_HOST']
url = f'https://{host}/twilio/voice'
params = {'CallSid': 'CA-test-0001', 'From': '+15551234567',
          'To': s.env['TWILIO_PHONE_NUMBER']}
sig = signed_payload(s.env['TWILIO_AUTH_TOKEN'], url, params)

req = urllib.request.Request(
    'http://127.0.0.1:8100/twilio/voice',
    data=urllib.parse.urlencode(params).encode(),
    headers={'X-Twilio-Signature': sig,
             'Content-Type': 'application/x-www-form-urlencoded'})
print(urllib.request.urlopen(req).read().decode())
"@
```

Expect TwiML containing `<Connect><Stream` and `wss://<host>/twilio/media-stream`.

If you get 403, the signature did not validate — check that `TUNNEL_HOST` has no scheme, no
trailing slash, and matches what you signed for. If you get 404, `TUNNEL_HOST` was unset when the
instance started, so the routes were never registered (deliberate: an endpoint that can only refuse
should not exist).

Run it twice and the second response is `<Response/>` with no `<Stream>` — that is replay
protection working, not a fault.

---

## Step 6 — Configure the number in Twilio

In the Twilio Console:

**Phone Numbers → Manage → Active numbers → +12792365960**

Under **Voice Configuration**:

| Field | Value |
|---|---|
| A call comes in | **Webhook** |
| URL | `https://<TUNNEL_HOST>/twilio/voice` |
| HTTP method | **POST** |
| Call status changes | `https://<TUNNEL_HOST>/twilio/status` |

Save. Nothing else on the number needs configuring — the media-stream WebSocket URL is derived from
`TUNNEL_HOST`, not set separately, because two settings that must agree are two settings that can
disagree.

---

## Step 7 — Place the call

Dial **+12792365960** and ask something answerable from the catalogue — *"how much is the
ultrasound gel"*.

Watch the instance terminal. A healthy call shows, in order: `media stream: started`, then
`VAD`/`USER_SPEECH_STARTED`, then the transcript, then `media stream: closed`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Twilio logs 403 | `TUNNEL_HOST` wrong, missing, or stale after a tunnel restart. Signature validation fails closed |
| Twilio logs 404 | `TUNNEL_HOST` was unset at boot; restart with it set |
| 403 with a *valid* signature | The `To` number is not the one registered. Attribution refuses a number this instance does not own |
| 502 / call fails immediately | Tunnel points at the wrong port — check it matches `ANITA_API_PORT` |
| Caller hears the busy message | Admission is full (`max_concurrent_calls`, default 2) or voice was shed by the VRAM ladder |
| Call connects, then silence | The STT or TTS leg. Run `scripts/voice_e2e.py --models` to test the models outside a call |
| Second identical test returns `<Response/>` | Replay protection. Correct behaviour — one `CallSid` opens one stream |

---

## What still needs a real call

**TAC-6 and AC-7 are implemented** (they were built in `4fe32b0`; this section used to say
otherwise). The 30-minute cap closes a silent call and retains the transcript; three unintelligible
turns end in a human-callback offer and the call closes. What no amount of local testing can
produce is the carrier's own evidence:

- a **real 34-character CallSid** in `inbound_event` — every id in the tables today is simulated;
- the timing budgets on a real network (p95 first-audio ≤ 1.2 s, refusal ≤ 150 ms, `T-01`/`T-17`);
- a real handset's audio for barge-in, gibberish and lead-capture behaviour.

The next section drives everything else without a carrier.

---

## Acceptance — T-01…T-17

Two halves, and both are needed before US-001 can be called done.

### Half 1 — drive Twilio's side yourself (no phone)

`scripts/carrier_acceptance.py` is the client a carrier would be: it signs with the real auth
token, downloads the TwiML, reads the `call_id`/`token` parameters out of it, opens the media
stream with them, and speaks a synthesised question into it.

```powershell
cd D:\project\anitamedicalsystems\platform

# preconditions only — is the tunnel up and the instance answering?
.venv\Scripts\python.exe scripts\carrier_acceptance.py --check

# everything that does not need a handset
.venv\Scripts\python.exe scripts\carrier_acceptance.py
```

| Check | What it proves |
|---|---|
| T-16 | A signature computed for the **public** URL validates; the TwiML hands out the stream |
| T-05 | An unsigned webhook is 403 with an empty body |
| T-06 | A withheld number still gets a stream (the session keys on the stream SID) |
| T-01 | The media round trip: synthesised speech → STT → answer → µ-law audio back |
| T-12 | The admission ceiling holds; the refused call gets busy TwiML and **no** `<Stream>` |
| T-13 | A dropped stream releases its slot; the next call is admitted |
| §8.6 | A tokenless `wss://` connection naming a live call is **closed with 1008** and releases nothing |

Exit code 0 means every check that ran passed; 2 means the preconditions were not met (usually a
stale `TUNNEL_HOST` — see the "one thing that bites" at the top).

**On this CPU-only host the ceiling is 1**, not 2 — the runner reports the effective number rather
than asserting a fixed one. A GPU host admits 2.

### Half 2 — the calls only a handset can make

Dial **+12792365960** and work through these, watching the instance terminal and
`logs/anita/perf_turns.jsonl`:

| # | Do this | Expect |
|---|---|---|
| T-02 | Interrupt the answer mid-sentence | Remaining clauses stop; one `clear`; transcript marked truncated |
| T-03 | "Can I speak to someone?" | Handoff row; the caller is told when to expect contact; the console Escalations panel shows it open |
| T-04 | Say nothing intelligible three times | Re-prompt, line-quality hint, then a human-callback offer; the call closes |
| T-07 | Give a name, no email | Lead saved partially and flagged |
| T-08 | Ask the same question twice | Answered twice, one lead (phone dedupe) |
| T-09 | Leave the line silent | The 30-minute cap closes the call; the transcript survives |
| T-10 | Stop Ollama, then ask a price | The price still answers; open prose questions degrade to the verbatim answer |
| T-14 | Call back within 6 h from the same number | The conversation and lead resume |
| T-15 | Hang up after an answer | Summary written from the stored transcript; `conversation.closed` fires once |

### Recording the evidence

After both halves, check the carrier's own traces landed:

```powershell
# the real CallSid the carrier sent — 34 chars, starting "CA"
.venv\Scripts\python.exe -c @"
import sqlite3
c = sqlite3.connect('logs/anita/anita_channels.sqlite')
for row in c.execute('SELECT provider_event_id, channel_kind, state FROM inbound_event ORDER BY rowid DESC LIMIT 5'):
    print(row)
"@
```

Then update `docs/sdlc/work-status.md` §3a: move `T-01…T-17` from outstanding to passed, record
the p95 first-audio figure from the traces, and re-class US-001 to `COMPLETED` only when a real
CallSid is in the table and the timing budgets hold.
