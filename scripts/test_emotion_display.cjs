// Exercise the actual room renderer with a small DOM double; no camera access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync("static/js/room.js", "utf8");
const renderSource = source.match(/  function renderEmotion\([^]*?\n  \}/)[0];

function setup() {
  const elements = new Map();
  const element = () => ({textContent: "", children: [], append(...items) {this.children.push(...items);}, replaceChildren() {this.children = [];}, setAttribute() {}});
  const card = {dataset: {}, classList: {remove() {}}, querySelector(selector) {if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector);}};
  let now = 100000;
  const context = vm.createContext({document: {querySelector: () => card, createElement: element}, Date: {now: () => now}});
  vm.runInContext(renderSource, context);
  return {card, elements, render: context.renderEmotion, tick: ms => {now += ms;}};
}
const success = {modality: "audio", state: "ok", scores: [{label_tr: "Sakin ton", score: .6}], detail: "Model skoru", latency_ms: 100};

test("silence preserves explicitly historical result without extending its expiry", () => {
  const {card, render, elements, tick} = setup();
  render(success);
  const timestamp = card.dataset.updatedAt;
  tick(5000);
  render({modality: "audio", state: "no_data", detail: "Sessizlik", scores: []});
  assert.equal(card.dataset.updatedAt, timestamp);
  assert.equal(elements.get(".emotion-label").textContent, "Son tahmin · Sakin ton");
  assert.equal(elements.get(".emotion-scores").children.length, 1);
  tick(20000);
  render({modality: "audio", state: "no_data", detail: "Sessizlik", scores: []});
  assert.equal(elements.get(".emotion-label").textContent, "Veri yok");
  assert.equal(elements.get(".emotion-scores").children.length, 0);
  assert.equal(card.dataset.updatedAt, undefined);
});

test("camera-off and errors clear previous scores immediately", () => {
  for (const message of [{modality: "video", state: "no_data", detail: "Kamera kapalı."}, {modality: "audio", state: "error"}]) {
    const {render, card, elements} = setup();
    render(success);
    render(message);
    assert.equal(card.dataset.updatedAt, undefined);
    assert.equal(elements.get(".emotion-scores").children.length, 0);
  }
});

test("received audio measurements distinguish no input from rejected input", () => {
  const {render, elements} = setup();
  render({modality: "audio", state: "no_data", detail: "Ses ulaştı.", signal: {duration_ms: 3000, rms_dbfs: -70, active_ms: 0}});
  assert.match(elements.get(".emotion-detail").textContent, /Sunucu: 3.0 sn ses/);
  assert.match(elements.get(".emotion-detail").textContent, /-70 dBFS/);
});
