const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("static/js/room.js", "utf8");
const styles = fs.readFileSync("static/css/app.css", "utf8");
const layoutSource = source.match(/  function setActiveParticipant\([^]*?(?=\n  function syncFullscreenButton)/)[0];

function classList() {
  const values = new Set();
  return {
    toggle(name, enabled) { enabled ? values.add(name) : values.delete(name); },
    contains(name) { return values.has(name); },
  };
}

function setup() {
  const tiles = ["local", "remote"].map((participant) => ({dataset: {participant}, classList: classList()}));
  const buttons = ["gallery", "speaker"].map((mode) => ({
    dataset: {viewMode: mode}, classList: classList(), attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
  }));
  const videoGrid = {dataset: {}, querySelectorAll: () => tiles};
  let now = 1000;
  const context = vm.createContext({
    videoGrid, document: {querySelectorAll: () => buttons}, isHost: true,
    activeParticipant: "remote", manualSpeakerUntil: 0,
    Date: {now: () => now}, remember() {},
  });
  vm.runInContext(layoutSource, context);
  return {context, tiles, buttons, videoGrid, tick(ms) { now += ms; }};
}

test("gallery and speaker controls update one video grid without replacing videos", () => {
  const {context, tiles, buttons, videoGrid} = setup();
  context.setViewMode("speaker");
  assert.equal(videoGrid.dataset.viewMode, "speaker");
  assert.equal(buttons[1].attributes["aria-pressed"], "true");
  assert.equal(tiles[1].classList.contains("is-active-speaker"), true);
  context.setViewMode("gallery");
  assert.equal(videoGrid.dataset.viewMode, "gallery");
});

test("speaker mode follows transcript roles and respects a short manual focus", () => {
  const {context, tiles, tick} = setup();
  context.updateActiveSpeaker("interviewer");
  assert.equal(tiles[0].classList.contains("is-active-speaker"), true);
  context.setActiveParticipant("remote", true);
  context.updateActiveSpeaker("interviewer");
  assert.equal(tiles[1].classList.contains("is-active-speaker"), true);
  tick(16000);
  context.updateActiveSpeaker("interviewer");
  assert.equal(tiles[0].classList.contains("is-active-speaker"), true);
});

test("candidate analysis stays consent-gated while only the host renders results", () => {
  assert.match(source, /const allowedHere = Boolean\(config\.candidateAnalysisConsent\)/);
  assert.match(source, /const lateEnable = Boolean\(message\.enabled && !allowedHere\)/);
  assert.match(source, /message\.type === "candidate-emotion" && isHost && analysisEnabled/);
  assert.match(source, /type: "analysis-consent", enabled: false/);
  assert.match(source, /const node = \$\("#live-captions"\);\n    if \(!node\) return;/);
});

test("permission card is forcibly hidden after media permission resolves", () => {
  assert.match(source, /prejoinCard\.hidden = true/);
  assert.match(source, /prejoinCard\.style\.display = "none"/);
  assert.match(source, /prejoinCard\.remove\(\)/);
  assert.match(styles, /\.prejoin-card\[hidden\]\s*\{\s*display:\s*none\s*!important;/);
});

test("media permission cannot leave the interface waiting forever", () => {
  assert.match(source, /requestMedia\([^]*?, 12000\)/);
  assert.match(source, /requestMedia\([^]*?video: false,[^]*?, 10000\)/);
  assert.match(source, /error\?\.name === "TimeoutError"/);
  assert.match(source, /startButton\.textContent = "Tekrar dene"/);
});

test("meeting video fills the complete stage beneath the transparent overlays", () => {
  assert.match(styles, /\.room-layout \{ min-height: 0; display: grid; \}/);
  assert.match(styles, /\.video-workspace \{[^}]*display: grid;[^}]*grid-template-rows: minmax\(0, 1fr\);[^}]*padding: 64px 16px 16px;/);
  assert.match(styles, /\.video-workspace:fullscreen \{[^}]*padding: 64px 18px 18px;/);
  assert.match(styles, /\.video-workspace \{ padding: 58px 8px 8px; \}/);
});
