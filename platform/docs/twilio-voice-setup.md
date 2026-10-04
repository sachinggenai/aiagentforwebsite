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

## What is still missing

Two acceptance criteria are **not** implemented, so a long or difficult call behaves worse than the
story specifies:

- **TAC-6** — the 30-minute call cap. A call with no successful end-of-speech is not closed
- **AC-7** — the three-strike path. After three unintelligible utterances the caller should be
  offered a human transfer or callback

Both are recorded in `docs/sdlc/work-status.md` §3a.
