// Aetherscope radio audio worklet.
//
// Receives mono Float32 PCM chunks (produced at `producerRate`, default
// 50 kHz) over the message port, buffers them, and plays them out at the
// AudioContext's own sample rate using linear-interpolation resampling.
// A latency cap drops the oldest audio if the buffer grows too large.

class RadioPlayer extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.producerRate = opts.producerRate || 50000;
    this.ratio = this.producerRate / sampleRate; // input samples per output sample
    this.buf = new Float32Array(0);
    this.readPos = 0;

    this.port.onmessage = (e) => {
      const d = e.data;
      if (d && d.type === "rate") {
        this.producerRate = d.rate || this.producerRate;
        this.ratio = this.producerRate / sampleRate;
        return;
      }
      if (d && d.type === "flush") {
        this.buf = new Float32Array(0);
        this.readPos = 0;
        return;
      }
      // Otherwise d is a Float32Array chunk: append it.
      const merged = new Float32Array(this.buf.length + d.length);
      merged.set(this.buf, 0);
      merged.set(d, this.buf.length);
      this.buf = merged;

      // Cap buffered audio to ~1 s to keep latency bounded.
      const cap = Math.floor(this.producerRate);
      const available = this.buf.length - Math.floor(this.readPos);
      if (available > cap) {
        const drop = this.buf.length - cap;
        this.buf = this.buf.slice(drop);
        this.readPos = Math.max(0, this.readPos - drop);
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;

    for (let i = 0; i < out.length; i++) {
      const idx = this.readPos;
      const i0 = Math.floor(idx);
      if (i0 + 1 >= this.buf.length) {
        out[i] = 0; // underrun -> silence
        continue;
      }
      const frac = idx - i0;
      out[i] = this.buf[i0] * (1 - frac) + this.buf[i0 + 1] * frac;
      this.readPos += this.ratio;
    }

    // Drop consumed samples to keep the buffer compact.
    const consumed = Math.floor(this.readPos);
    if (consumed > 0) {
      this.buf = this.buf.slice(consumed);
      this.readPos -= consumed;
    }
    return true;
  }
}

registerProcessor("radio-player", RadioPlayer);
