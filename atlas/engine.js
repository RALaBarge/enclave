const DATA = /*DATA*/null;
const RUNTIME = /*RUNTIME*/null;

const css = getComputedStyle(document.documentElement);
const AREA = {};
for (const a of ["console","harness","image","sbx","tools","common","root","deskbox","runtime"]) AREA[a] = css.getPropertyValue("--a-" + a).trim();
const AREA_NAME = { console: "Firewall console", harness: "Harness (bridge, broker, gate…)", image: "Image & install", sbx: "Sandbox box",
  tools: "Sandbox tools", common: "Shared", root: "Repo root", deskbox: "deskbox (agent-desk)", runtime: "Runtime" };
const INK = css.getPropertyValue("--ink").trim();
const ACCENT = css.getPropertyValue("--accent").trim(), OK = css.getPropertyValue("--ok").trim(),
      BAD = css.getPropertyValue("--bad").trim(), WARN = css.getPropertyValue("--warn").trim();
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const areaOf = (comp) => comp.split("/")[0] === "deskbox" ? "deskbox" : comp.split("/")[0];

// ---------- build nodes and links (same model as the library version)
const nodes = [], links = [], byId = new Map();
function node(n) { n.i = nodes.length; byId.set(n.id, n); nodes.push(n); return n; }
const areas = new Set(), comps = new Set();
for (const f of DATA.files) { comps.add(f.comp); areas.add(areaOf(f.comp)); }
for (const a of areas) node({ id: "area:" + a, t: "area", name: AREA_NAME[a] || a, area: a, val: 60 });
for (const c of comps) {
  const a = areaOf(c);
  if (c !== a) { node({ id: "comp:" + c, t: "comp", name: c, area: a, val: 18 }); links.push({ source: "area:" + a, target: "comp:" + c, t: "contain" }); }
}
const compNode = (c) => (byId.has("comp:" + c) ? "comp:" + c : "area:" + areaOf(c));
for (const f of DATA.files) {
  const a = areaOf(f.comp);
  node({ id: "file:" + f.id, t: "file", name: f.id.split("/").pop(), path: f.id, area: a, lang: f.lang, lines: f.lines, head: f.head,
         fix: f.fix, test: /(^|\/)test_|\.test\.|_test\.go$|smoke-test|\/tests\//.test(f.id), val: 3 + Math.min(9, f.lines / 180) });
  links.push({ source: compNode(f.comp), target: "file:" + f.id, t: "contain" });
}
const symCount = {};
for (const s of DATA.symbols) {
  const fnode = byId.get("file:" + s.file);
  node({ id: "sym:" + s.id, t: "sym", name: s.name, kind: s.kind, path: s.file, line: s.line, end: s.end, sig: s.sig, doc: s.doc,
    parent: s.parent, area: fnode.area, test: s.test || fnode.test, tested: s.tested, unused: s.unused, fix: s.fix,
    callers: s.callers, callees: s.callees, val: s.kind === "class" || s.kind === "type" ? 1.6 : 0.6 });
  symCount[s.file] = (symCount[s.file] || 0) + 1;
}
const classIn = new Map();
for (const s of DATA.symbols) if (s.kind === "class" || s.kind === "type") classIn.set(s.file + "#" + s.name, "sym:" + s.id);
for (const s of DATA.symbols) {
  const holder = s.parent && classIn.get(s.file + "#" + s.parent);
  links.push({ source: holder || "file:" + s.file, target: "sym:" + s.id, t: "contain" });
}
for (const [a, b] of DATA.calls) links.push({ source: "sym:" + a, target: "sym:" + b, t: "call" });
const symFile = new Map(DATA.symbols.map((s) => [s.id, s.file]));
const fcalls = new Map();
for (const [a, b] of DATA.calls) {
  const fa = symFile.get(a), fb = symFile.get(b);
  if (fa && fb && fa !== fb) { const k = fa + "\u0000" + fb; fcalls.set(k, (fcalls.get(k) || 0) + 1); }
}
for (const [k, count] of fcalls) { const [fa, fb] = k.split("\u0000"); links.push({ source: "file:" + fa, target: "file:" + fb, t: "fcall", count }); }
for (const r of RUNTIME.nodes) {
  node({ id: "rt:" + r.id, t: "rt", name: r.name, area: "runtime", where: r.where, what: r.what, val: r.kind === "machine" ? 40 : 10, rkind: r.kind });
  for (const f of r.files || []) if (byId.has("file:" + f)) links.push({ source: "rt:" + r.id, target: "file:" + f, t: "impl" });
  if (r.on) links.push({ source: "rt:" + r.on, target: "rt:" + r.id, t: "host" });
}
for (const [a, b, label, off] of RUNTIME.flows) links.push({ source: "rt:" + a, target: "rt:" + b, t: "flow", label, off: !!off });

const nbr = new Map(), callIn = new Map(), callOut = new Map(), flowIn = new Map(), flowOut = new Map();
const push = (m, k, v) => { (m.get(k) || m.set(k, []).get(k)).push(v); };
for (const l of links) {
  push(nbr, l.source, l); push(nbr, l.target, l);
  if (l.t === "call") { push(callOut, l.source, l.target); push(callIn, l.target, l.source); }
  if (l.t === "flow") { push(flowOut, l.source, l); push(flowIn, l.target, l); }
}

// ---------- stats and meta
const syms = DATA.symbols, fnLike = syms.filter((s) => !s.test && ["function", "method", "shell function"].includes(s.kind));
$("meta").textContent = `enclave ${DATA.meta.enclave} · tools ${DATA.meta.tools} · deskbox ${DATA.meta.deskbox} · single-draw renderer`;
$("stats").innerHTML = [[DATA.files.length, "files"], [syms.filter((s) => s.kind !== "class" && s.kind !== "type").length, "functions"],
  [syms.filter((s) => s.kind === "class" || s.kind === "type").length, "classes"], [DATA.calls.length, "calls"]]
  .map(([n, l]) => `<div class="stat"><b>${n.toLocaleString()}</b><span>${l}</span></div>`).join("");
$("c-calls").textContent = DATA.calls.length; $("c-tests").textContent = syms.filter((s) => s.test).length;
$("c-rt").textContent = RUNTIME.nodes.length; $("c-dbx").textContent = syms.filter((s) => s.file.startsWith("deskbox/")).length;

// ---------- state and styling rules
const state = { view: "explore", calls: true, tests: true, runtime: true, dbx: true, collapse: false, hidden: new Set(), sel: null, hl: new Set(), hlLinks: new Set() };
const idOf = (x) => (typeof x === "object" ? x.id : x);
const srcOf = (l) => (typeof l.source === "object" ? l.source : byId.get(l.source));
const tgtOf = (l) => (typeof l.target === "object" ? l.target : byId.get(l.target));
function visibleNode(n) {
  if (state.view === "explore") return levelNodes.has(n.id);
  if (state.collapse && n.t === "sym") return false;
  if (state.hidden.has(n.area)) return false;
  if (!state.tests && n.test) return false;
  if (!state.runtime && n.t === "rt") return false;
  if (!state.dbx && n.area === "deskbox") return false;
  if (state.view === "runtime") return n.t === "rt" || n.t === "file" && (nbr.get(n.id) || []).some((l) => l.t === "impl");
  if (state.view === "findings") return n.t !== "sym" || n.fix || n.unused || state.hl.has(n.id) || n.kind === "class";
  return true;
}
function visibleLink(l) {
  if (!l._sv || !l._tv) return false;          // endpoint visibility, cached per pass
  if (state.view === "explore") return levelLinks.has(l);
  if (l.t === "call" && (!state.calls || state.collapse)) return false;
  if (l.t === "fcall" && !(state.collapse && state.calls)) return false;
  if (state.view === "runtime" && l.t === "contain") return false;
  return true;
}
function colorOf(n) {
  if (state.sel && state.hl.size && !state.hl.has(n.id)) return "#3a4558";
  if (state.view === "findings" && n.t === "sym") return n.fix ? OK : n.unused ? BAD : AREA[n.area];
  if (n.t === "rt") return AREA.runtime;
  return AREA[n.area] || "#9aa7bb";
}
function linkColor(l) {
  if (l.t === "flow") return l.off ? "#f4efe266" : "#f4efe2";
  if (l.t === "call") return state.sel ? "#ffffff10" : "#8fb3ff40";
  if (l.t === "fcall") return state.sel ? "#ffffff14" : "#8fb3ff" + Math.min(255, 70 + l.count * 12).toString(16).padStart(2, "0");
  if (l.t === "impl") return "#f4efe255";
  if (l.t === "host") return "#f4efe230";
  return "#ffffff1c";
}

let flat = false;
// ---------- layers: start at the running system, go in one step at a time
const levels = [{ k: "root" }];
const cur = () => levels[levels.length - 1];
let levelNodes = new Set(), levelLinks = new Set();
const kidsOf = (id) => (nbr.get(id) || []).filter((l) => l.t === "contain" && idOf(l.source) === id).map((l) => idOf(l.target));
const kindFor = (n) => ({ rt: "rt", area: "area", comp: "comp", file: "file", sym: "sym" }[n.t]);
const LEVEL_TYPES = { root: ["flow", "host"], rt: ["flow", "host", "impl"], area: ["contain"], comp: ["contain"],
  file: ["contain", "call", "fcall", "impl"], sym: ["call", "contain"] };
function computeLevel() {
  const lv = cur(), set = new Set();
  const add = (id) => { const n = byId.get(id); if (n && (state.tests || !n.test || id === lv.id) && (state.dbx || n.area !== "deskbox" || id === lv.id)) set.add(id); };
  const both = (l) => { add(idOf(l.source)); add(idOf(l.target)); };
  if (lv.k === "root") { for (const n of nodes) if (n.t === "rt" && state.runtime || n.t === "area" && !state.hidden.has(n.area)) add(n.id); }
  else if (lv.k === "rt") { add(lv.id); for (const l of nbr.get(lv.id) || []) if (l.t === "flow" || l.t === "host" || l.t === "impl") both(l); }
  else if (lv.k === "area" || lv.k === "comp") { add(lv.id); for (const k of kidsOf(lv.id)) add(k); }
  else if (lv.k === "file") {
    add(lv.id);
    for (const k of kidsOf(lv.id)) { add(k); for (const k2 of kidsOf(k)) add(k2); }
    for (const l of nbr.get(lv.id) || []) if (l.t === "fcall" || l.t === "impl") both(l);
  } else if (lv.k === "sym") {
    add(lv.id);
    for (const i of callIn.get(lv.id) || []) add(i);
    for (const i of callOut.get(lv.id) || []) add(i);
    for (const l of nbr.get(lv.id) || []) if (l.t === "contain" && idOf(l.target) === lv.id) add(idOf(l.source));
  }
  const types = new Set(LEVEL_TYPES[lv.k]);
  levelNodes = set;
  levelLinks = new Set(links.filter((l) => types.has(l.t) && set.has(idOf(l.source)) && set.has(idOf(l.target))));
}
function crumbName(lv) {
  if (lv.k === "root") return "System";
  const n = byId.get(lv.id);
  return n.t === "sym" && n.parent ? n.parent + "." + n.name : n.t === "comp" ? n.name.split("/").slice(1).join("/") || n.name : n.name;
}
function renderCrumbs() {
  const c = $("crumbs");
  if (state.view !== "explore") { c.hidden = true; return; }
  c.hidden = false;
  const hint = { root: "White dots are the running services; coloured dots are folders. Click one to go in.",
    rt: "The services it talks to and the files that implement it. Click a file to open it.",
    area: "Its components and files. Click one to open it.", comp: "Its files. Click one to open it.",
    file: "Its functions and classes, the calls between them, and the files it calls or is called from.",
    sym: "Who calls it and what it calls. Click any of them to follow the call." }[cur().k];
  c.innerHTML = `<button class="back" data-up ${levels.length < 2 ? "disabled" : ""} aria-label="Up one layer">←</button>` +
    `<div class="trail">` + levels.map((lv, i) => i === levels.length - 1
      ? `<span class="here">${esc(crumbName(lv))}</span>`
      : `<button data-lv="${i}">${esc(crumbName(lv))}</button><span class="sep">›</span>`).join("") + `</div>` +
    `<div class="hint">${esc(hint)} <b>${levelNodes.size}</b> shown.</div>`;
}
function applyLevel() {
  computeLevel();
  const arr = nodes.filter((n) => levelNodes.has(n.id)), focus = cur().id && byId.get(cur().id);
  for (const n of arr) {                         // a fresh, small layout around the focus
    if (n === focus) { n.x = n.y = 0; n.z = 0; }
    else { const a = Math.random() * Math.PI * 2, b = Math.acos(2 * Math.random() - 1), r = 60 + Math.random() * 120;
      n.x = r * Math.sin(b) * Math.cos(a); n.y = r * Math.sin(b) * Math.sin(a); n.z = flat ? 0 : r * Math.cos(b); }
    n.vx = n.vy = n.vz = 0;
  }
  sim.nodes(arr);
  sim.force("link").links([...levelLinks]);
  reheat(1);
  state.sel = null; state.hl = new Set(); state.hlLinks = new Set();
  refresh(); updateVisibility(); setLabels();
  renderCrumbs();
  if (focus) { renderDrawer(focus); $("drawer").hidden = false; } else $("drawer").hidden = true;
  flyTo(new T.Vector3(0, 0, 700), new T.Vector3(0, 0, 0), reduced ? 0 : 500);
  setTimeout(() => zoomToFit(reduced ? 0 : 700), 1200);
}
function drill(id) {
  const n = byId.get(id); if (!n || !kindFor(n)) return;
  const at = levels.findIndex((lv) => lv.id === id);
  if (at >= 0) levels.length = at + 1;           // already on the trail: go back to it
  else levels.push({ k: kindFor(n), id });
  applyLevel();
}
function up(to) { levels.length = Math.max(1, to ?? levels.length - 1); applyLevel(); }
// the natural trail to any node, for search: System > area > component > file > function
function pathTo(n) {
  const out = [{ k: "root" }];
  if (n.t === "rt") return [...out, { k: "rt", id: n.id }];
  const area = "area:" + n.area;
  if (n.t === "area") return [...out, { k: "area", id: n.id }];
  const file = n.t === "file" ? n : n.t === "sym" ? byId.get("file:" + n.path) : null;
  const compId = n.t === "comp" ? n.id : file && (nbr.get(file.id) || []).map((l) => idOf(l.source)).find((i) => i.startsWith("comp:"));
  out.push({ k: "area", id: area });
  if (compId) out.push({ k: "comp", id: compId });
  if (file) out.push({ k: "file", id: file.id });
  if (n.t === "sym") out.push({ k: "sym", id: n.id });
  return out;
}
function jumpTo(id) { const n = byId.get(id); if (!n) return; levels.length = 0; levels.push(...pathTo(n)); applyLevel(); }

// ---------- three.js: one instanced mesh for every node, one line buffer for every link
const T = THREE;
const el = $("graph");
const renderer = new T.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
el.appendChild(renderer.domElement);
const scene = new T.Scene();
const ink = new T.Color(INK);
scene.background = ink;
const camera = new T.PerspectiveCamera(50, 1, 1, 40000);
camera.position.set(0, 0, 1600);
const controls = new T.OrbitControls(camera, renderer.domElement);
controls.enableDamping = true; controls.dampingFactor = 0.12; controls.screenSpacePanning = true;
scene.add(new T.AmbientLight(0xffffff, 0.75));
const sun = new T.DirectionalLight(0xffffff, 0.6); sun.position.set(1, 1.4, 1); scene.add(sun);

const N = nodes.length;
const sphere = new T.SphereGeometry(1, 12, 8);
const nodeMesh = new T.InstancedMesh(sphere, new T.MeshLambertMaterial({ color: 0xffffff }), N);
nodeMesh.instanceMatrix.setUsage(T.DynamicDrawUsage);
nodeMesh.frustumCulled = false;
scene.add(nodeMesh);
const radius = (n) => 3.2 * Math.cbrt(n.val);

// colors for lines: blend "#rrggbbaa" over the ink so a single opaque buffer can carry per-link strength
const _c = new T.Color();
function rgbaOver(hex, out) {
  const a = hex.length > 7 ? parseInt(hex.slice(7, 9), 16) / 255 : 1;
  _c.set(hex.slice(0, 7));
  out[0] = ink.r + (_c.r - ink.r) * a; out[1] = ink.g + (_c.g - ink.g) * a; out[2] = ink.b + (_c.b - ink.b) * a;
}
const L = links.length;
const linePos = new Float32Array(L * 6), lineCol = new Float32Array(L * 6);
const lineGeo = new T.BufferGeometry();
lineGeo.setAttribute("position", new T.BufferAttribute(linePos, 3).setUsage(T.DynamicDrawUsage));
lineGeo.setAttribute("color", new T.BufferAttribute(lineCol, 3).setUsage(T.DynamicDrawUsage));
const lines = new T.LineSegments(lineGeo, new T.LineBasicMaterial({ vertexColors: true }));
lines.frustumCulled = false;
scene.add(lines);
// highlighted links: a second, small buffer drawn on top in the accent color
const HMAX = 4000;
const hlPos = new Float32Array(HMAX * 6);
const hlGeo = new T.BufferGeometry();
hlGeo.setAttribute("position", new T.BufferAttribute(hlPos, 3).setUsage(T.DynamicDrawUsage));
hlGeo.setDrawRange(0, 0);
const hlLines = new T.LineSegments(hlGeo, new T.LineBasicMaterial({ color: new T.Color(ACCENT), depthTest: false, transparent: true }));
hlLines.frustumCulled = false; hlLines.renderOrder = 2;
scene.add(hlLines);
// moving dots on the runtime flows (and on highlighted calls): one point cloud
const flowLinks = links.filter((l) => l.t === "flow" && !l.off);
const PMAX = 600;
const pPos = new Float32Array(PMAX * 3);
const pGeo = new T.BufferGeometry();
pGeo.setAttribute("position", new T.BufferAttribute(pPos, 3).setUsage(T.DynamicDrawUsage));
pGeo.setDrawRange(0, 0);
const dots = new T.Points(pGeo, new T.PointsMaterial({ color: new T.Color(ACCENT), size: 3.2, sizeAttenuation: true, depthTest: false, transparent: true }));
dots.frustumCulled = false; dots.renderOrder = 3;
scene.add(dots);

// ---------- force layout (d3-force-3d, the same engine the library wraps)
const sim = d3.forceSimulation(nodes, 3)
  .alphaDecay(0.035).velocityDecay(0.35)
  .force("charge", d3.forceManyBody().strength((n) => (n.t === "area" ? -900 : n.t === "rt" ? -260 : n.t === "comp" ? -200 : n.t === "file" ? -60 : -14)).theta(0.9))
  .force("link", d3.forceLink(links).id((n) => n.id)
    .distance((l) => (l.t === "contain" ? ({ area: 90, comp: 46, file: 16 }[srcOf(l).t] || 12) : l.t === "flow" ? 70 : l.t === "host" ? 55 : l.t === "impl" ? 80 : 40))
    .strength(linkStrength))
  .force("center", d3.forceCenter())
  .stop();
function linkStrength(l) { return l.t === "call" ? (state.collapse ? 0 : 0.04) : l.t === "fcall" ? (state.collapse ? 0.05 : 0) : l.t === "impl" ? 0.08 : l.t === "flow" ? 0.5 : 0.9; }
for (let i = 0; i < 60; i++) sim.tick();       // warm up before the first frame
let simRunning = true, simStopAt = performance.now() + 7000;
sim.on("tick", () => { posDirty = true; });
function reheat(alpha = 0.6) { sim.alpha(alpha); simRunning = true; simStopAt = performance.now() + 5000; }

// ---------- buffer updates
let posDirty = true, styleDirty = true, visDirty = true, labelsDirty = true;
const _m = new T.Matrix4(), _q = new T.Quaternion(), _s = new T.Vector3(), _p = new T.Vector3();
const nodeVis = new Uint8Array(N), rgb = [0, 0, 0];
function updateVisibility() {
  for (const n of nodes) nodeVis[n.i] = visibleNode(n) ? 1 : 0;
  for (const l of links) { l._sv = nodeVis[srcOf(l).i]; l._tv = nodeVis[tgtOf(l).i]; l._vis = visibleLink(l); }
  visDirty = false; posDirty = true; styleDirty = true; labelsDirty = true;
}
function updatePositions() {
  for (const n of nodes) {
    const r = nodeVis[n.i] ? radius(n) : 0;
    _p.set(n.x || 0, n.y || 0, n.z || 0); _s.set(r, r, r);
    nodeMesh.setMatrixAt(n.i, _m.compose(_p, _q, _s));
  }
  nodeMesh.instanceMatrix.needsUpdate = true;
  for (let k = 0; k < L; k++) {
    const l = links[k], s = l.source, t = l.target, o = k * 6;
    if (l._vis) { linePos[o] = s.x; linePos[o + 1] = s.y; linePos[o + 2] = s.z || 0; linePos[o + 3] = t.x; linePos[o + 4] = t.y; linePos[o + 5] = t.z || 0; }
    else { linePos[o] = linePos[o + 3] = s.x; linePos[o + 1] = linePos[o + 4] = s.y; linePos[o + 2] = linePos[o + 5] = s.z || 0; }
  }
  lineGeo.attributes.position.needsUpdate = true;
  updateHighlightBuffer();
  posDirty = false; labelsDirty = true;
}
function updateStyle() {
  for (const n of nodes) { nodeMesh.setColorAt(n.i, _c.set(colorOf(n))); }
  nodeMesh.instanceColor.needsUpdate = true;
  for (let k = 0; k < L; k++) {
    rgbaOver(linkColor(links[k]), rgb);
    const o = k * 6;
    lineCol[o] = lineCol[o + 3] = rgb[0]; lineCol[o + 1] = lineCol[o + 4] = rgb[1]; lineCol[o + 2] = lineCol[o + 5] = rgb[2];
  }
  lineGeo.attributes.color.needsUpdate = true;
  updateHighlightBuffer();
  styleDirty = false; labelsDirty = true;
}
let hlList = [];
function updateHighlightBuffer() {
  hlList = [...state.hlLinks].filter((l) => l._vis).slice(0, HMAX);
  hlList.forEach((l, k) => { const s = l.source, t = l.target, o = k * 6;
    hlPos[o] = s.x; hlPos[o + 1] = s.y; hlPos[o + 2] = s.z || 0; hlPos[o + 3] = t.x; hlPos[o + 4] = t.y; hlPos[o + 5] = t.z || 0; });
  hlGeo.setDrawRange(0, hlList.length * 2);
  hlGeo.attributes.position.needsUpdate = true;
}
function updateDots(now) {
  if (reduced) { pGeo.setDrawRange(0, 0); return false; }
  const moving = flowLinks.filter((l) => l._vis).concat(hlList.filter((l) => l.t === "call" || l.t === "fcall"));
  let k = 0;
  for (const l of moving) {
    for (let j = 0; j < 2 && k < PMAX; j++, k++) {
      const f = ((now / 2600) + j / 2 + (l.source.i % 7) / 7) % 1, s = l.source, t = l.target;
      pPos[k * 3] = s.x + (t.x - s.x) * f; pPos[k * 3 + 1] = s.y + (t.y - s.y) * f; pPos[k * 3 + 2] = (s.z || 0) + ((t.z || 0) - (s.z || 0)) * f;
    }
  }
  pGeo.setDrawRange(0, k); pGeo.attributes.position.needsUpdate = true;
  return k > 0;
}

// ---------- camera helpers
function resize() {
  const w = innerWidth, h = innerHeight;
  renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); labelsDirty = true;
}
addEventListener("resize", resize); resize();
let tween = null;
function flyTo(pos, target, ms = 900) {
  if (reduced || !ms) { camera.position.copy(pos); controls.target.copy(target); controls.update(); labelsDirty = true; return; }
  tween = { t0: performance.now(), ms, p0: camera.position.clone(), p1: pos.clone(), c0: controls.target.clone(), c1: target.clone() };
}
function zoomToFit(ms = 800, filter = (n) => nodeVis[n.i]) {
  const vis = nodes.filter((n) => filter(n) && n.x !== undefined);
  if (!vis.length) return;
  const c = new T.Vector3();
  for (const n of vis) c.add(_p.set(n.x, n.y, n.z || 0));
  c.divideScalar(vis.length);
  let r = 1;
  for (const n of vis) r = Math.max(r, _p.set(n.x, n.y, n.z || 0).distanceTo(c) + radius(n));
  const dist = r / Math.sin((camera.fov * Math.PI) / 360) * 1.05;
  const dir = camera.position.clone().sub(controls.target).normalize();
  if (!isFinite(dir.x)) dir.set(0, 0, 1);
  flyTo(c.clone().add(dir.multiplyScalar(dist)), c, ms);
}

// ---------- picking: one raycast against the instanced mesh
const ray = new T.Raycaster(), ptr = new T.Vector2();
let hoverN = null, lastMove = null, downAt = null;
function pick(e) {
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  // Analytic ray-sphere test per node (the stock instanced raycast tests every triangle of every sphere).
  const o = ray.ray.origin, d = ray.ray.direction;
  let best = null, bestT = Infinity;
  for (const n of nodes) {
    if (!nodeVis[n.i]) continue;
    const r = radius(n) * 1.15, cx = n.x - o.x, cy = n.y - o.y, cz = (n.z || 0) - o.z;
    const t = cx * d.x + cy * d.y + cz * d.z;
    if (t <= 0) continue;
    const d2 = cx * cx + cy * cy + cz * cz - t * t;
    if (d2 <= r * r && t < bestT) { bestT = t; best = n; }
  }
  return best;
}
renderer.domElement.addEventListener("pointermove", (e) => { lastMove = e; });
renderer.domElement.addEventListener("pointerleave", () => { lastMove = null; hover(null); });
renderer.domElement.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
  const n = pick(e);
  if (state.view === "explore") { if (n && n.id !== cur().id) drill(n.id); else if (!n) select(null); return; }
  select(n ? n.id : null, !!n);
});

// ---------- tooltip
const tip = $("tip");
function hover(n) {
  if (n === hoverN) return;
  hoverN = n;
  renderer.domElement.style.cursor = n ? "pointer" : "";
  if (!n) { tip.hidden = true; return; }
  const sub = n.t === "sym" ? `${n.kind} · ${n.path}:${n.line}` : n.t === "file" ? `${n.lang} · ${n.lines} lines` : n.t === "rt" ? n.where : n.t === "comp" ? "component" : "area";
  tip.innerHTML = `${esc(n.t === "file" ? n.path : n.name)}<small>${esc(sub)}</small>`;
  tip.hidden = false;
}
addEventListener("pointermove", (e) => { if (tip.hidden) return; tip.style.left = Math.min(innerWidth - 350, e.clientX + 14) + "px"; tip.style.top = e.clientY + 14 + "px"; });

// ---------- labels
const labelEls = new Map(), lblBox = $("labels"), labelState = new Map();
// In a layer every dot gets a name (up to 180); in the full views only areas, components and services do.
function setLabels() {
  lblBox.textContent = ""; labelEls.clear(); labelState.clear();
  const pick = state.view === "explore" ? nodes.filter((n) => levelNodes.has(n.id)).slice(0, 180)
    : nodes.filter((n) => n.t === "area" || n.t === "comp" || n.t === "rt");
  const focusId = cur().id;
  for (const n of pick) {
    const d = document.createElement("div");
    d.className = "lbl " + (n.t === "area" ? "area" : n.t === "rt" ? "rt" : n.t === "sym" ? "sym" : n.t === "file" ? "file" : "");
    if (n.id === focusId && state.view === "explore") d.classList.add("focus");
    d.textContent = n.t === "comp" ? n.name.split("/").slice(1).join("/") || n.name : n.t === "sym" && n.parent ? n.parent + "." + n.name : n.name;
    if (n.t === "area") d.style.color = AREA[n.area];
    lblBox.appendChild(d); labelEls.set(n, d); labelState.set(d, { shown: true, x: 0, y: 0, o: 1 });
  }
  labelsDirty = true;
}
const _v = new T.Vector3(), _dir = new T.Vector3();
function updateLabels() {
  camera.getWorldDirection(_dir);
  const w = innerWidth / 2, h = innerHeight / 2;
  for (const [n, d] of labelEls) {
    const st = labelState.get(d);
    let show = nodeVis[n.i] && n.x !== undefined && !(state.sel && state.hl.size && !state.hl.has(n.id) && n.t !== "area");
    let dist = 0;
    if (show) { _v.set(n.x, n.y, n.z || 0).sub(camera.position); dist = _v.length(); show = _v.dot(_dir) > 0; }
    if (!show) { if (st.shown) { d.style.display = "none"; st.shown = false; } continue; }
    if (!st.shown) { d.style.display = ""; st.shown = true; }
    _v.set(n.x, n.y, n.z || 0).project(camera);
    const x = Math.round((_v.x + 1) * w), y = Math.round((1 - _v.y) * h);
    if (x !== st.x || y !== st.y) { d.style.transform = `translate(${x}px, ${y}px) translate(-50%, -140%)`; st.x = x; st.y = y; }
    const o = n.t === "comp" && state.view !== "explore" ? Math.round(Math.max(0.25, Math.min(1, 900 / dist)) * 10) / 10 : 1;
    if (o !== st.o) { d.style.opacity = o; st.o = o; }
  }
  labelsDirty = false;
}

// ---------- one loop: step the layout while it moves, update only what changed, draw
let camKey = "";
function frame(now) {
  requestAnimationFrame(frame);
  if (document.hidden) return;
  if (simRunning) { sim.tick(); posDirty = true; if (sim.alpha() < sim.alphaMin() || now > simStopAt) simRunning = false; }
  if (tween) {
    const k = Math.min(1, (now - tween.t0) / tween.ms), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    camera.position.lerpVectors(tween.p0, tween.p1, e); controls.target.lerpVectors(tween.c0, tween.c1, e);
    if (k >= 1) tween = null;
  }
  controls.update();
  if (visDirty) updateVisibility();
  if (styleDirty) updateStyle();
  if (posDirty) updatePositions();
  const anim = updateDots(now);
  const key = camera.position.toArray().map((v) => v.toFixed(1)).join() + camera.quaternion.toArray().map((v) => v.toFixed(4)).join();
  if (key !== camKey) { camKey = key; labelsDirty = true; }
  if (labelsDirty) updateLabels();
  if (lastMove) { hover(pick(lastMove)); lastMove = null; }
  renderer.render(scene, camera);
}
requestAnimationFrame(frame);
// the first layer is applied at the end of the script, once every handler exists
const refresh = () => { visDirty = true; styleDirty = true; };
const refreshStyle = () => { styleDirty = true; };
document.addEventListener("visibilitychange", () => { labelsDirty = true; });

// ---------- selection and inspector
function select(id, fly) {
  state.sel = id; state.hl = new Set(); state.hlLinks = new Set();
  const dr = $("drawer");
  if (!id) { dr.hidden = true; state.view === "findings" ? refresh() : refreshStyle(); return; }
  let n = byId.get(id);
  state.hl.add(id);
  for (const l of nbr.get(id) || []) { state.hlLinks.add(l); state.hl.add(idOf(l.source)); state.hl.add(idOf(l.target)); }
  if (n.t === "file") for (const l of nbr.get(id) || []) if (l.t === "contain" && idOf(l.source) === id)
    for (const l2 of nbr.get(idOf(l.target)) || []) { state.hl.add(idOf(l2.source)); state.hl.add(idOf(l2.target)); if (l2.t === "call") state.hlLinks.add(l2); }
  state.view === "findings" ? refresh() : refreshStyle();
  renderDrawer(byId.get(id));
  dr.hidden = false;
  if (fly && state.collapse && n.t === "sym") n = byId.get("file:" + n.path) || n;
  if (fly && n.x !== undefined) {
    const dist = n.t === "area" ? 420 : n.t === "rt" && n.rkind === "machine" ? 380 : n.t === "comp" ? 220 : 120;
    const target = new T.Vector3(n.x, n.y, n.z || 0);
    const dir = camera.position.clone().sub(target).normalize();
    flyTo(target.clone().add(dir.multiplyScalar(dist)), target);
  }
}
const chip = (cls, t) => `<span class="chip ${cls}">${esc(t)}</span>`;
function listOf(ids, title) {
  if (!ids || !ids.length) return "";
  const items = [...new Set(ids)].map((i) => byId.get(i)).filter(Boolean)
    .sort((a, b) => (a.path || "").localeCompare(b.path || "") || a.name.localeCompare(b.name));
  return `<div class="eyebrow">${esc(title)} · ${items.length}</div><div class="linklist">` + items.map((m) =>
    `<button data-go="${esc(m.id)}"><span class="rname">${esc(m.parent ? m.parent + "." + m.name : m.name)}</span><span class="rpath">${esc(m.t === "sym" ? m.path + ":" + m.line : m.t === "file" ? m.path : m.where || "")}</span></button>`).join("") + "</div>";
}
function renderDrawer(n) {
  let h = `<div class="dr-head"><h2>${esc(n.t === "sym" && n.parent ? n.parent + "." + n.name : n.name)}</h2><button class="x" data-close aria-label="Close">×</button></div>`;
  const chips = [];
  const kindLabel = { area: "area", comp: "component", file: "file · " + n.lang, rt: "runtime · " + n.rkind }[n.t] || n.kind;
  chips.push(chip("", kindLabel));
  if (n.t !== "rt") chips.push(`<span class="chip" style="color:${AREA[n.area]};border-color:${AREA[n.area]}66">${esc(AREA_NAME[n.area] || n.area)}</span>`);
  if (n.fix) chips.push(chip("fix", "fixed 2026-11-15"));
  if (n.unused) chips.push(chip("unused", "nothing calls this"));
  if (n.t === "sym" && !n.test && ["function", "method", "shell function"].includes(n.kind)) chips.push(n.tested ? chip("tested", "reached by tests") : chip("untested", "no direct test path"));
  if (n.test) chips.push(chip("", "test code"));
  h += `<div class="chips">${chips.join("")}</div>`;
  if (n.t === "sym") {
    h += `<div class="loc">${esc(n.path)}:${n.line}${n.end && n.end !== n.line ? "–" + n.end : ""}</div>`;
    if (n.sig) h += `<pre class="sig">${esc(n.sig)}</pre>`;
    h += n.doc ? `<div class="doc">${esc(n.doc)}</div>` : `<div class="note">No docstring or leading comment.</div>`;
    if (n.fix) h += `<div class="fixnote">${esc(n.fix)}</div>`;
    h += listOf(callIn.get(n.id), "Called by") + listOf(callOut.get(n.id), "Calls");
    if (!callIn.get(n.id) && !n.unused && !n.test) h += `<div class="note">No direct caller found by the static pass; it is reached from module-level code, a route table, a callback, HTTP or another language.</div>`;
    h += `<button class="x" style="font-size:12px;align-self:flex-start" data-go="file:${esc(n.path)}">Open file ${esc(n.path.split("/").pop())} →</button>`;
  } else if (n.t === "file") {
    h += `<div class="loc">${esc(n.path)}</div>`;
    if (n.head) h += `<div class="doc">${esc(n.head)}</div>`;
    if (n.fix) h += `<div class="fixnote">${esc(n.fix)}</div>`;
    h += `<dl class="kv"><dt>Lines</dt><dd>${n.lines.toLocaleString()}</dd><dt>Symbols</dt><dd>${symCount[n.path] || 0}</dd></dl>`;
    const kids = (nbr.get(n.id) || []).filter((l) => l.t === "contain" && idOf(l.source) === n.id).map((l) => idOf(l.target));
    const grand = kids.flatMap((k) => (nbr.get(k) || []).filter((l) => l.t === "contain" && idOf(l.source) === k).map((l) => idOf(l.target)));
    h += listOf([...kids, ...grand], "Defined here");
    const fl = (nbr.get(n.id) || []).filter((l) => l.t === "fcall");
    const fmt = (arr, dirOut) => arr.sort((a, b) => b.count - a.count).map((l) => { const o = byId.get(idOf(dirOut ? l.target : l.source));
      return `<button data-go="${esc(o.id)}"><span class="rname">${dirOut ? "→" : "←"} ${esc(o.name)}</span><span class="rpath">${l.count} call${l.count === 1 ? "" : "s"} · ${esc(o.path)}</span></button>`; }).join("");
    const fo = fl.filter((l) => idOf(l.source) === n.id), fi = fl.filter((l) => idOf(l.target) === n.id);
    if (fo.length) h += `<div class="eyebrow">Calls into · ${fo.length} files</div><div class="linklist">${fmt(fo, true)}</div>`;
    if (fi.length) h += `<div class="eyebrow">Called from · ${fi.length} files</div><div class="linklist">${fmt(fi, false)}</div>`;
    h += listOf((nbr.get(n.id) || []).filter((l) => l.t === "impl").map((l) => idOf(l.source)), "Runs as");
  } else if (n.t === "rt") {
    h += `<div class="loc">${esc(n.where)}</div><div class="doc">${esc(n.what)}</div>`;
    const fo = flowOut.get(n.id) || [], fi = flowIn.get(n.id) || [];
    if (fo.length || fi.length) {
      h += `<div class="eyebrow">Talks to</div><div class="linklist">` +
        fo.map((l) => `<button data-go="${esc(idOf(l.target))}"><span class="rname">→ ${esc(byId.get(idOf(l.target)).name)}</span><span class="rpath">${esc(l.label)}${l.off ? " (off)" : ""}</span></button>`).join("") +
        fi.map((l) => `<button data-go="${esc(idOf(l.source))}"><span class="rname">← ${esc(byId.get(idOf(l.source)).name)}</span><span class="rpath">${esc(l.label)}${l.off ? " (off)" : ""}</span></button>`).join("") + `</div>`;
    }
    h += listOf((nbr.get(n.id) || []).filter((l) => l.t === "impl").map((l) => idOf(l.target)), "Code");
    h += listOf((nbr.get(n.id) || []).filter((l) => l.t === "host" && idOf(l.source) === n.id).map((l) => idOf(l.target)), "Runs here");
  } else {
    const kids = (nbr.get(n.id) || []).filter((l) => l.t === "contain" && idOf(l.source) === n.id).map((l) => idOf(l.target));
    h += listOf(kids, n.t === "area" ? "Components and files" : "Files");
  }
  $("dr").innerHTML = h;
}
$("drawer").addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]"); if (g) return state.view === "explore" ? drill(g.dataset.go) : select(g.dataset.go, true);
  if (e.target.closest("[data-close]")) select(null);
});

// ---------- search
const index = nodes.map((n) => ({ n, k: (n.name + " " + (n.path || "") + " " + (n.parent || "") + " " + (n.where || "")).toLowerCase() }));
$("q").addEventListener("input", () => {
  const q = $("q").value.trim().toLowerCase(), box = $("results");
  if (!q) { box.innerHTML = ""; return; }
  const hits = index.filter((x) => x.k.includes(q)).map((x) => x.n)
    .sort((a, b) => (b.name.toLowerCase() === q) - (a.name.toLowerCase() === q) || (b.name.toLowerCase().startsWith(q)) - (a.name.toLowerCase().startsWith(q)) || a.name.length - b.name.length)
    .slice(0, 40);
  box.innerHTML = hits.length ? hits.map((n) => `<button data-go="${esc(n.id)}" role="option"><span class="sw" style="background:${n.t === "rt" ? AREA.runtime : AREA[n.area]}"></span><span class="rname">${esc(n.parent ? n.parent + "." + n.name : n.name)}</span><span class="rpath">${esc(n.t === "sym" ? n.path + ":" + n.line : n.t === "file" ? n.path : n.where || n.t)}</span></button>`).join("")
    : `<div class="note" style="padding:4px 6px">Nothing matches “${esc(q)}”.</div>`;
});
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") { const b = $("results").querySelector("button"); if (b) b.click(); } });
$("results").addEventListener("click", (e) => { const g = e.target.closest("[data-go]"); if (!g) return; state.view === "explore" ? jumpTo(g.dataset.go) : select(g.dataset.go, true); });
$("crumbs").addEventListener("click", (e) => {
  if (e.target.closest("[data-up]")) return up();
  const b = e.target.closest("[data-lv]"); if (b) up(+b.dataset.lv + 1);
});
addEventListener("keydown", (e) => {
  if (state.view !== "explore" || e.target.closest("input, textarea")) return;
  if (e.key === "Escape" || e.key === "Backspace") { if (levels.length > 1) { e.preventDefault(); up(); } }
});

// ---------- views, layers, legend, flat, collapse
const NOTES = { explore: "Layers: start from what runs, then go in to files, functions and calls. Use the trail at the bottom to come back out.",
  all: "Every file and function, with calls between them and the services they run as.",
  runtime: "How a prompt moves at run time: you → pi-bridge → Pi → the desk → the broker → over the cable to sbx. Dim lines are switched off today.",
  findings: "Green: changed by the 2026-11-15 security fixes. Red: nothing calls it. Everything else is hidden except classes." };
function setView(v) {
  const was = state.view;
  state.view = v;
  if (v === "explore") { renderCrumbs(); applyLevel(); for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === v)); $("viewNote").textContent = NOTES[v]; return; }
  if (was === "explore") {                        // back to the whole graph: every node, every link, a full re-layout
    sim.nodes(nodes); sim.force("link").links(links); reheat(1);
    $("drawer").hidden = true; state.sel = null; state.hl = new Set(); state.hlLinks = new Set();
  }
  renderCrumbs();
  for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === v));
  $("viewNote").textContent = NOTES[v];
  refresh(); updateVisibility(); setLabels();
  setTimeout(() => zoomToFit(reduced ? 0 : 800), was === "explore" ? 1600 : 0);
}
for (const b of document.querySelectorAll("[data-view]")) b.addEventListener("click", () => setView(b.dataset.view));
$("viewNote").textContent = NOTES.explore;
const bind = (id, key) => $(id).addEventListener("change", (e) => { state[key] = e.target.checked; state.view === "explore" ? applyLevel() : refresh(); });
bind("l-calls", "calls"); bind("l-tests", "tests"); bind("l-runtime", "runtime"); bind("l-dbx", "dbx");
$("legend").innerHTML = [...areas, "runtime"].map((a) => `<button data-area="${a}" aria-pressed="true"><span class="sw" style="background:${AREA[a]}"></span>${esc(AREA_NAME[a] || a)}</button>`).join("");
$("legend").addEventListener("click", (e) => {
  const b = e.target.closest("[data-area]"); if (!b) return;
  const a = b.dataset.area, on = state.hidden.has(a);
  if (a === "runtime") { state.runtime = on; $("l-runtime").checked = on; }
  else on ? state.hidden.delete(a) : state.hidden.add(a);
  b.setAttribute("aria-pressed", String(on)); state.view === "explore" ? applyLevel() : refresh();
});
$("railToggle").addEventListener("click", () => $("rail").classList.toggle("collapsed"));
if (innerWidth <= 760) $("rail").classList.add("collapsed");
$("collapse").addEventListener("click", () => {
  state.collapse = !state.collapse;
  const b = $("collapse");
  b.setAttribute("aria-pressed", String(state.collapse));
  b.textContent = state.collapse ? "Show functions" : "Collapse into files";
  sim.force("link").strength(linkStrength);
  refresh();
  if (state.sel) select(state.sel, false);
  reheat(0.5);
  setTimeout(() => zoomToFit(reduced ? 0 : 700), 1400);
});
$("flat").addEventListener("click", () => {
  flat = !flat;
  const b = $("flat");
  b.setAttribute("aria-pressed", String(flat));
  b.textContent = flat ? "Back to 3D" : "Flatten to top-down";
  if (flat) for (const n of nodes) { n.z = 0; n.vz = 0; }
  sim.numDimensions(flat ? 2 : 3);
  if (!flat) for (const n of nodes) { n.z = (Math.random() - 0.5) * 40; n.vz = 0; }
  controls.enableRotate = !flat;
  reheat(flat ? 0.4 : 0.8);
  const target = controls.target.clone().setZ(0);
  const d = camera.position.distanceTo(controls.target);
  flyTo(flat ? new T.Vector3(target.x, target.y, d) : new T.Vector3(target.x + d * 0.55, target.y + d * 0.25, d * 0.75), target, reduced ? 0 : 800);
  setTimeout(() => zoomToFit(reduced ? 0 : 700), 1600);
});

// ---------- findings summary
(() => {
  const fixed = nodes.filter((n) => n.fix), unused = nodes.filter((n) => n.unused);
  const enc = fnLike.filter((s) => !s.file.startsWith("deskbox/")), dbx = fnLike.filter((s) => s.file.startsWith("deskbox/"));
  const pct = (a) => Math.round(100 * a.filter((s) => s.tested).length / a.length);
  const gaps = Object.entries(enc.filter((s) => !s.tested).reduce((m, s) => ((m[s.file] = (m[s.file] || 0) + 1), m), {}))
    .sort((a, b) => b[1] - a[1]).slice(0, 5);
  $("findings").innerHTML = `<div class="eyebrow">Findings</div>
    <div class="row"><span class="n" style="color:${OK}">${fixed.length}</span><span>places changed by today's security fixes. <button class="link" data-v="findings">Show them</button></span></div>
    <div class="row"><span class="n" style="color:${BAD}">${unused.length}</span><span>function${unused.length === 1 ? "" : "s"} nothing calls: ${unused.map((u) => `<button class="link" data-go="${esc(u.id)}">${esc(u.name)}</button>`).join(", ")}</span></div>
    <div class="row"><span class="n" style="color:${WARN}">${pct(enc)}%</span><span>of Enclave's functions sit on a direct call path from a test (deskbox: ${pct(dbx)}%). The rest are tested over HTTP or by the smoke test, or not at all. Biggest gaps: ${gaps.map(([f, c]) => `<button class="link" data-go="file:${esc(f)}">${esc(f.split("/").pop())}</button> (${c})`).join(", ")}.</span></div>
    <div class="note">This copy draws every node in one instanced mesh and every link in one line buffer: about five draw calls a frame, whatever the size.</div>`;
  $("findings").addEventListener("click", (e) => {
    const g = e.target.closest("[data-go]"); if (g) return state.view === "explore" ? jumpTo(g.dataset.go) : select(g.dataset.go, true);
    if (e.target.closest("[data-v]")) setView("findings");
  });
})();

// ---------- start on the top layer
setView("explore");
