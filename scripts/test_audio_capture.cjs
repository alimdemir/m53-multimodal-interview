const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

for (const rate of [16000, 44100, 48000]) {
  test(`AudioWorklet ${rate} Hz -> continuous 16 kHz PCM`, () => {
    const packets = [];
    let Processor;
    const context = {
      sampleRate: rate, Int16Array, Math,
      AudioWorkletProcessor: class { constructor() { this.port = {postMessage: buffer => packets.push(new Int16Array(buffer))}; } },
      registerProcessor: (_, cls) => { Processor = cls; },
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/audio-capture.js'), 'utf8'), context);
    const processor = new Processor();
    for (let start = 0; start < rate * 2; start += 128) {
      const input = new Float32Array(Math.min(128, rate * 2 - start)).fill(.5);
      assert.equal(processor.process([[input]]), true);
    }
    assert.equal(packets.length, Math.floor(32000 / 1024));
    assert.equal(processor.index, 32000 % 1024);
    for (const packet of packets) assert.ok(packet.every(value => value === 16383));
    const remainder = processor.index;
    processor.port.onmessage({data: {type: 'finish'}});
    // The audio tail is posted before the explicit end marker.
    assert.equal(packets.at(-2).length, remainder);
    assert.equal(processor.index, 0);
    assert.equal(processor.process([[new Float32Array(128)]]), false);
  });
}
