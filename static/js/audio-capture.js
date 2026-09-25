/* Persistent, fractional downsampling. Output PCM16 LE at 16 kHz, ~64 ms packets. */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.weight = 0;
    this.sum = 0;
    this.packet = new Int16Array(1024);
    this.index = 0;
    this.finished = false;
    this.port.onmessage = ({data}) => {
      if (data?.type !== "finish" || this.finished) return;
      this.finished = true;
      if (this.index) {
        const tail = this.packet.slice(0, this.index);
        this.port.postMessage(tail.buffer, [tail.buffer]);
      }
      this.index = 0;
      this.port.postMessage({type: "capture-finished"});
    };
  }
  process(inputs) {
    if (this.finished) return false;
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) {
      let remaining = 1;
      while (remaining > 1e-8) {
        const part = Math.min(remaining, this.ratio - this.weight);
        this.sum += sample * part;
        this.weight += part;
        remaining -= part;
        if (this.weight >= this.ratio - 1e-8) {
          const value = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.packet[this.index++] = value < 0 ? value * 32768 : value * 32767;
          this.weight = 0;
          this.sum = 0;
          if (this.index === this.packet.length) {
            this.port.postMessage(this.packet.buffer, [this.packet.buffer]);
            this.packet = new Int16Array(1024);
            this.index = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
