/* 浏览器端实时重采样：44.1k/48k 单声道 Float32 → 16kHz Int16 PCM。
   每 3200 样本（200ms / 6400 字节）打包一次，正好是豆包官方建议的分包大小。
   只做搬运与重采样，不做任何 VAD / 业务判断。 */
class Pcm16kProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunkSize = 3200;          // 16000 * 0.2s
    this.buf = new Float32Array(this.chunkSize);
    this.idx = 0;
    this.frac = 0;                  // 重采样相位
    this.frames = 0;
    this.peak = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length || !input[0] || !input[0].length) return true;
    const ch = input[0];
    const ratio = sampleRate / 16000;

    let pos = this.frac;
    while (pos < ch.length) {
      const i = Math.floor(pos);
      const f = pos - i;
      const a = ch[i];
      const b = i + 1 < ch.length ? ch[i + 1] : a;
      const s = a * (1 - f) + b * f;
      const abs = Math.abs(s);
      if (abs > this.peak) this.peak = abs;
      this.buf[this.idx++] = s;
      if (this.idx >= this.chunkSize) {
        const pcm = new Int16Array(this.chunkSize);
        for (let k = 0; k < this.chunkSize; k++) {
          let v = Math.max(-1, Math.min(1, this.buf[k]));
          pcm[k] = v < 0 ? v * 0x8000 : v * 0x7FFF;
        }
        this.port.postMessage({ type: 'pcm', buffer: pcm.buffer }, [pcm.buffer]);
        this.idx = 0;
      }
      pos += ratio;
    }
    this.frac = pos - ch.length;

    // 每 ~12 帧（约 100ms）回送一次音量，供前端画电平条
    if (++this.frames % 12 === 0) {
      this.port.postMessage({ type: 'level', rms: this.peak });
      this.peak = 0;
    }
    return true;
  }
}
registerProcessor('pcm16k', Pcm16kProcessor);
