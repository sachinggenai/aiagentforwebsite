"""
The audio format the raw-PCM path speaks — one home for it.

`/ws/voice` and the browser page it serves have to agree on this, and they used
to state it independently: the endpoint in a docstring, the page in JavaScript,
and the u-law conversions as a bare `2` at each call site.

These were four `Settings` fields (`AUDIO_SAMPLE_RATE`, `AUDIO_CHANNELS`,
`AUDIO_SAMPLE_WIDTH`, `CHUNK_FRAMES`) until US-011 clause 4 asked what read them.
Nothing did — and they are not settings either. `CHANNELS` and `SAMPLE_WIDTH` are
invariants of the codecs in use (audioop's u-law conversions are mono only, and
this pipeline is 16-bit linear PCM), and `CHUNK_FRAMES` is 20 ms of audio, which
follows the rate rather than being independently settable. A key that cannot be
changed without breaking the audio is not a knob, so they live here rather than
in `app/config.py`, where every key is environment-backed and can be set without
editing code.

Twilio's leg is deliberately separate: its wire format is 8 kHz u-law, and
`app/voice_handler.py` resamples it up to `SAMPLE_RATE` for the pipeline.
"""

SAMPLE_RATE = 16000        # Hz — the raw-PCM websocket path (browser ↔ /ws/voice)
CHANNELS = 1               # mono — audioop's u-law conversions are mono only
SAMPLE_WIDTH = 2           # bytes per sample — the pipeline's 16-bit linear PCM
CHUNK_MS = 20              # one frame of audio
CHUNK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000   # derived, so it cannot desync (320)
