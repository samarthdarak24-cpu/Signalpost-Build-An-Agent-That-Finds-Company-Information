/* Render every view against the live API in a minimal fake DOM.
 * Catches runtime errors (undefined access, bad template construction) that a
 * syntax check cannot. Not a browser — it verifies the view functions produce
 * HTML without throwing and that the HTML is non-empty and balanced. */
import { readFileSync } from "fs";

const BASE = "http://127.0.0.1:8787";

function makeEl(tag = "div") {
  const el = {
    tagName: tag.toUpperCase(), children: [], attrs: {}, dataset: {}, style: {},
    _html: "", hidden: false, classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    textContent: "",
    querySelector(sel) {
      // Resolve against the markup this element currently holds, so a view's
      // own $("#x", wrap) resolves the way it would in a browser.
      const id = String(sel).match(/^#([\w-]+)$/);
      const cls = String(sel).match(/^\.([\w-]+)$/);
      const re = id ? new RegExp(`id="${id[1]}"`) : (cls ? new RegExp(`class="[^"]*\\b${cls[1]}\\b`) : null);
      if (re && re.test(this._html)) return makeEl("div");
      return null;
    },
    querySelectorAll(sel) {
      const out = [];
      const cls = String(sel).match(/^\.([\w-]+)$/);
      if (cls) {
        const n = (this._html.match(new RegExp(`class="[^"]*\\b${cls[1]}\\b`, "g")) || []).length;
        for (let i = 0; i < n; i++) out.push(makeEl("div"));
      }
      return out;
    },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, remove() {},
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    insertAdjacentHTML() {}, scrollIntoView() {}, focus() {}, blur() {},
    closest() { return null; }, contains() { return false; },
    lastElementChild: null, offsetWidth: 1, value: "",
    insertBefore() {}, replaceChild() {},
  };
  return el;
}

const registry = new Map();
function getEl(id) {
  if (!registry.has(id)) registry.set(id, makeEl(id === "main" ? "main" : "div"));
  return registry.get(id);
}

const win = globalThis;
win.addEventListener = () => {};
win.removeEventListener = () => {};
win.__lastFocus = null;
globalThis.window = win;
globalThis.document = {
  documentElement: makeEl("html"),
  body: makeEl("body"),
  title: "",
  querySelector: (s) => getEl(s.replace(/^#/, "")),
  querySelectorAll: () => [],
  createElement: (t) => makeEl(t),
  addEventListener: () => {},
  activeElement: null,
};
globalThis.location = { hash: "#/overview", href: BASE };
globalThis.localStorage = { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = v; } };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
const realFetch = globalThis.fetch;
globalThis.fetch = (p, o) => realFetch(String(p).startsWith("http") ? p : BASE + p, o);
globalThis.console.error = (...a) => { throw new Error("console.error: " + a.join(" ")); };

const src = readFileSync("web-next/app.js", "utf8");
// Expose the view functions for direct invocation.
const patched = src + `
;globalThis.__views = { viewOverview, viewSearch, viewCompany, viewCoverage, viewTrace,
  viewAsk, viewReports, viewSources, viewAuditor, viewLive, route, state, ensureProfiles };
;
`;
const mod = new Function(patched + "\nreturn globalThis.__views;");

const views = mod();
await new Promise(r => setTimeout(r, 900)); // let boot() settle

let failures = 0;
const checks = [];
function check(name, ok, detail = "") {
  checks.push([ok ? "PASS" : "FAIL", name, detail]);
  if (!ok) failures++;
}

await views.ensureProfiles();
check("profiles loaded", views.state.profiles.length > 0, `n=${views.state.profiles.length}`);

const hashRoutes = [
  ["#/overview", "viewOverview", []],
  ["#/search", "viewSearch", []],
  ["#/company", "viewCompany", [undefined, undefined]],
  ["#/company/923609016", "viewCompany", ["923609016", "overview"]],
  ["#/company/923609016/evidence", "viewCompany", ["923609016", "evidence"]],
  ["#/company/923609016/registry", "viewCompany", ["923609016", "registry"]],
  ["#/company/923609016/people", "viewCompany", ["923609016", "people"]],
  ["#/company/923609016/accounts", "viewCompany", ["923609016", "accounts"]],
  ["#/company/923609016/web", "viewCompany", ["923609016", "web"]],
  ["#/company/923609016/history", "viewCompany", ["923609016", "history"]],
  ["#/coverage", "viewCoverage", []],
  ["#/trace", "viewTrace", [undefined]],
  ["#/trace/923609016", "viewTrace", ["923609016"]],
  ["#/ask", "viewAsk", []],
  ["#/reports", "viewReports", []],
  ["#/sources", "viewSources", []],
  ["#/auditor", "viewAuditor", []],
  ["#/live", "viewLive", []],
];

for (const [hash, fn, args] of hashRoutes) {
  try {
    const html = await views[fn](...args);
    const len = (html || "").length;
    const empty = len < 120;
    check(`${hash} renders`, !empty, `${len} chars`);
    if (!empty) {
      // crude tag balance for the tags we generate most
      for (const tag of ["div", "section", "table", "tr", "td", "span"]) {
        const o = (html.match(new RegExp(`<${tag}[\\s>]`, "g")) || []).length;
        const c = (html.match(new RegExp(`</${tag}>`, "g")) || []).length;
        if (o !== c) check(`${hash} <${tag}> balanced`, false, `${o} open / ${c} close`);
      }
      if (/undefined|\[object Object\]|NaN/.test(html)) {
        const hit = html.match(/.{0,60}(undefined|\[object Object\]|NaN).{0,60}/)?.[0];
        check(`${hash} has no undefined/NaN leak`, false, hit);
      }
    }
  } catch (e) {
    check(`${hash} renders`, false, e.message);
  }
}

console.log("hash            status  detail");
let last = "";
for (const [st, name, detail] of checks) {
  if (name !== last) { console.log(""); last = name; }
  if (st === "FAIL") console.log(`  ${st}  ${name}  ${detail}`);
}
console.log(`\n${checks.filter(c => c[0] === "PASS").length} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
