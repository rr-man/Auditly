// Parses the page's flowchart source with the vendored Mermaid, outside a browser.
// usage: node tests/mermaid_parse.js auditly.html static/mermaid.min.js
// Extracts mmLabel()/flowSource() from the page, runs them with stub inputs (default and demo),
// and asks Mermaid to parse each result -- the same parser the browser runs, so a diagram that
// would show "Parse error" to users fails here first. Exit 0 = every variant parsed.
const fs = require("fs");
const [html, lib] = process.argv.slice(2);
// the least DOM Mermaid (and its bundled sanitizer) need to load and parse
global.window = global; global.self = global; global.navigator = { userAgent: "node" };
global.addEventListener = () => {}; global.removeEventListener = () => {}; global.location = { href: "http://localhost/" };
global.document = { nodeType: 9, createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, getElementsByTagName: () => [], classList: { add() {} } }),
  createElementNS: () => ({ style: {}, setAttribute() {}, appendChild() {} }), createTextNode: () => ({}), querySelector: () => null, querySelectorAll: () => [],
  body: { appendChild() {}, removeChild() {} }, documentElement: { style: {} }, addEventListener() {}, getElementsByTagName: () => [],
  implementation: { createHTMLDocument() { return { body: {}, createElement: () => ({}) }; } } };
global.DOMParser = class { parseFromString() { return { body: { childNodes: [] }, documentElement: {} }; } };
for (const k of ["Element", "Node", "DocumentFragment", "HTMLTemplateElement", "HTMLFormElement", "HTMLElement", "SVGElement"]) global[k] = class {};
global.NodeFilter = {};
// the bundle is "use strict", so its top-level var does not become a global under eval: bind it ourselves
(0, eval)(fs.readFileSync(lib, "utf8").replace("globalThis.mermaid = globalThis.__esbuild_esm_mermaid.default;", "globalThis.mermaid = __esbuild_esm_mermaid.default;"));
const page = fs.readFileSync(html, "utf8");
const code = page.slice(page.indexOf("  function mmLabel("), page.indexOf("  function renderFlowInto("));
const stts = [{ id: "deepgram:nova-3", label: "Deepgram nova-3", note: "speaker labels · timestamps · $0.0077/min" },
              { id: "openai:whisper-1", label: "OpenAI whisper-1", note: "timestamps · no speaker labels · $0.006/min" },
              { id: "openai:gpt-4o-transcribe", label: "OpenAI gpt-4o-transcribe", note: "newer · no speaker labels · $0.006/min" }];
const S = { defaultStt: "deepgram:nova-3", health: { openai: { scoring_model: "gpt-4o-mini" }, scoring_choices: [{ model: "gpt-4o-mini" }, { model: "gpt-5.6-luna" }, { model: "gpt-4.1-mini" }] } };
const flowSource = new Function("S", "sttChoices", "esc", code + "\nreturn flowSource;")(S, () => stts, (s) => String(s));
(async () => {
  let bad = 0;
  for (const [name, o] of [["default", {}], ["highlight second engine", { highlight: "openai:whisper-1" }], ["demo run", { demo: true }], ["top-to-bottom (portrait window)", { direction: "TB" }], ["demo top-to-bottom", { demo: true, direction: "TB" }], ["no health yet", null]]) {
    const src = o === null ? new Function("S", "sttChoices", "esc", code + "\nreturn flowSource;")({ defaultStt: null, health: null }, () => stts, String)({}) : flowSource(o);
    try { const r = await globalThis.mermaid.parse(src); console.log("OK   " + name + " -> " + r.diagramType); }
    catch (e) { bad++; console.log("FAIL " + name + " -> " + String(e.message || e).split("\n").filter(Boolean).slice(-1)[0]); console.log(src); }
  }
  process.exit(bad ? 1 : 0);
})();
