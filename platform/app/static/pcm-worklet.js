// Capture worklet for browser-mic voice (US-008, TAC-2).
//
// Runs on the audio thread: takes whatever the device gives (usually 44.1 or
// 48 kHz float) and posts mono 16 kHz **PCM16** to the page, which is the
// frozen client→server contract. The conversion happens here rather than on
// the main thread because a busy main thread drops audio, and dropped audio in
// the middle of a specification name is a misheard question.
//
// Downsampling averages each group of input samples (box filter) instead of
// keeping every Nth. Keeping every Nth is cheaper and aliases — energy above
// 8 kHz folds back into the speech band — and the failure looks like a
// recogniser that is merely mediocre rather than a bug with a cause.

class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.ratio = sampleRate / this.targetRate;   // `sampleRate` is a global here
    this.pending = [];        // input samples awaiting a full output bucket
    this.pendingLen = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length || !input[0]) return true;
    const channel = input[0];

    for (let i = 0; i < channel.length; i++) {
      this.pending.push(channel[i]);
    }
    this.pendingLen = this.pending.length;

    const outCount = Math.floor(this.pendingLen / this.ratio);
    if (outCount < 1) return true;

    const out = new Int16Array(outCount);
    let cursor = 0;
    for (let n = 0; n < outCount; n++) {
      let sum = 0;
      const end = cursor + this.ratio;
      const limit = Math.min(Math.floor(end), this.pendingLen);
      let count = 0;
      for (let i = cursor; i < limit; i++) { sum += this.pending[i]; count++; }
      const sample = count ? sum / count : 0;
      const clamped = Math.max(-1, Math.min(1, sample));
      out[n] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF;
      cursor = Math.floor(end);
    }
    this.pending = this.pending.slice(cursor);

    this.port.postMessage(out, [out.buffer]);
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
