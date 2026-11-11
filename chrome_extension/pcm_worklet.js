class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._targetSamples = Math.max(320, Math.round(sampleRate * 0.04)); // ~40ms (1920 mẫu @ 48kHz)
    this._buffer = new Int16Array(this._targetSamples);
    this._writeIdx = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || !input[0]) {
      return true;
    }

    const numChannels = input.length;
    const frameCount = input[0].length;

    for (let i = 0; i < frameCount; i++) {
      let sum = 0;
      for (let ch = 0; ch < numChannels; ch++) {
        sum += input[ch][i] || 0;
      }
      const mono = Math.max(-1, Math.min(1, sum / numChannels));
      this._buffer[this._writeIdx++] = mono < 0
        ? Math.round(mono * 32768)
        : Math.round(mono * 32767);

      if (this._writeIdx >= this._targetSamples) {
        const chunk = this._buffer;
        this.port.postMessage(chunk.buffer, [chunk.buffer]);
        this._buffer = new Int16Array(this._targetSamples);
        this._writeIdx = 0;
      }
    }

    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);

