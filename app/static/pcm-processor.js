// AudioWorklet: takes mic float32 samples at the context rate, downsamples to 16 kHz,
// converts to 16-bit PCM and posts ~100 ms chunks to the main thread.
class PCMProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.inRate = sampleRate; // AudioContext rate (often 48000)
    this.outRate = 16000;
    this.ratio = this.inRate / this.outRate;
    this.buffer = [];
    this.chunkSamples = Math.round(this.outRate * 0.1); // 100 ms
    this.pos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];
    // Simple decimation with linear interpolation.
    while (this.pos < ch.length) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = ch[i];
      const b = i + 1 < ch.length ? ch[i + 1] : a;
      const s = a + (b - a) * frac;
      this.buffer.push(Math.max(-1, Math.min(1, s)));
      this.pos += this.ratio;
    }
    this.pos -= ch.length;
    if (this.buffer.length >= this.chunkSamples) {
      const out = new Int16Array(this.buffer.length);
      for (let i = 0; i < this.buffer.length; i++) {
        const v = this.buffer[i];
        out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      this.port.postMessage(out.buffer, [out.buffer]);
      this.buffer = [];
    }
    return true;
  }
}
registerProcessor('pcm-processor', PCMProcessor);
