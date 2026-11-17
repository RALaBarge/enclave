// ---------- tree view: the repo drawn top-down, repo → areas → components → files → functions.
// An add-on to the Atlas engine: it reads the engine's nodes and "contain" links, and plugs into select() and setView().
(() => {
  const idv = (x) => (typeof x === "object" ? x.id : x);
  const ROOT = "tree:root";
  const h1 = document.querySelector("#rail h1");
  const root = { id: ROOT, t: "root", name: (h1 ? h1.textContent : "Repo").replace(/\s*Atlas\s*$/i, "") || "Repo" };
  const get = (id) => (id === ROOT ? root : byId.get(id));

  // hierarchy from the contain links; areas hang off the repo
  const kids = new Map(), par = new Map();
  const addKid = (a, b) => { (kids.get(a) || kids.set(a, []).get(a)).push(b); par.set(b, a); };
  for (const n of nodes) if (n.t === "area") addKid(ROOT, n.id);
  for (const l of links) if (l.t === "contain") { const a = idv(l.source), b = idv(l.target); if (byId.has(a) && byId.has(b)) addKid(a, b); }
  const RANK = { area: 0, comp: 1, file: 2, sym: 3 };
  const key = (n) => String(n.path || n.name || "");
  for (const [k, arr] of kids) if (k !== ROOT) arr.sort((a, b) => {
    const A = byId.get(a), B = byId.get(b);
    return (RANK[A.t] - RANK[B.t]) || (A.t === "sym" && B.t === "sym" ? (A.line || 0) - (B.line || 0) : key(A).localeCompare(key(B)));
  });

  // static counts for the meta line
  const cnt = new Map();
  (function count(id) {
    const n = get(id); let f = n.t === "file" ? 1 : 0, s = n.t === "sym" && n.kind !== "class" && n.kind !== "type" ? 1 : 0;
    for (const k of kids.get(id) || []) { const c = count(k); f += c.f; s += c.s; }
    const c = { f, s }; cnt.set(id, c); return c;
  })(ROOT);

  // the same filters the rail applies elsewhere: hidden areas, tests, tools, deskbox
  const shown = (n) => !(state.hidden && state.hidden.has(n.area)) && (state.tests !== false || !n.test)
    && (typeof isTool !== "function" || state.tools !== false || !isTool(n.area))
    && (state.dbx !== false || n.area !== "deskbox");
  const disp = (id) => (kids.get(id) || []).filter((k) => shown(byId.get(k)));

  // ---------- DOM
  const pane = document.createElement("div");
  pane.id = "tree"; pane.setAttribute("aria-label", "The repo as a tree, top down");
  pane.innerHTML = `<div id="treeWorld"><svg id="treeEdges"></svg><svg id="treeCalls"></svg><div id="treeNodes"></div></div>`;
  const g3 = document.getElementById("graph");
  g3.parentNode.insertBefore(pane, g3.nextSibling);
  const bar = document.createElement("nav");
  bar.id = "treeBar"; bar.className = "panel"; bar.setAttribute("aria-label", "Tree controls");
  bar.innerHTML = `<button data-t="fit">Fit</button><button data-t="files">Open all files</button><button data-t="areas">Close to areas</button>
    <button class="zm" data-t="out" aria-label="Zoom out">−</button><button class="zm" data-t="in" aria-label="Zoom in">+</button>
    <span class="hint" id="treeHint"></span>`;
  document.body.appendChild(bar);
  const world = pane.querySelector("#treeWorld"), eSvg = pane.querySelector("#treeEdges"), cSvg = pane.querySelector("#treeCalls"), nBox = pane.querySelector("#treeNodes");

  // the Tree button, second in the view row
  const views = document.querySelector(".views[role=group]");
  const tb = document.createElement("button");
  tb.dataset.view = "tree"; tb.setAttribute("aria-pressed", "false"); tb.textContent = "Tree";
  views.insertBefore(tb, views.children[1] || null);
  tb.addEventListener("click", () => setView("tree"));

  // ---------- layout: branches spread sideways, leaves stack in columns under their parent
  const W = 232, GX = 22, GY = 44, SG = 6, IND = 18, PER_COL = 14;
  const H = { root: 50, area: 46, comp: 42, file: 42, sym: 28 };
  const hOf = (id) => H[get(id).t] || 40;
  const open = new Set([ROOT]);
  for (const k of kids.get(ROOT) || []) open.add(k);
  let M = new Map(), P = new Map(), edges = [], size = { w: 0, h: 0 };

  function measure(id) {
    const h0 = hOf(id), ks = open.has(id) ? disp(id) : [];
    if (!ks.length) { M.set(id, { w: W, h: h0, cols: [], branches: [] }); return M.get(id); }
    const leaves = [], branches = [];
    for (const k of ks) (open.has(k) && disp(k).length ? branches : leaves).push(k);
    const cols = [];
    if (leaves.length) {
      const nc = Math.ceil(leaves.length / PER_COL), per = Math.ceil(leaves.length / nc);
      for (let i = 0; i < leaves.length; i += per) cols.push(leaves.slice(i, i + per));
    }
    const items = [];
    for (const c of cols) items.push({ w: IND + W, h: c.reduce((s, k) => s + hOf(k), 0) + SG * (c.length - 1), col: c });
    for (const b of branches) { const m = measure(b); items.push({ w: m.w, h: m.h, b }); }
    const rowW = items.reduce((s, it) => s + it.w, 0) + GX * (items.length - 1), rowH = Math.max(...items.map((it) => it.h));
    const m = { w: Math.max(W, rowW), h: h0 + GY + rowH, rowW, items };
    M.set(id, m); return m;
  }
  function place(id, x, y) {
    const m = M.get(id), h0 = hOf(id), nx = x + (m.w - W) / 2;
    P.set(id, { x: nx, y, w: W, h: h0 });
    if (!m.items) return;
    const px = nx + W / 2, py = y + h0, busY = py + GY / 2;
    let cx = x + (m.w - m.rowW) / 2;
    const cy = py + GY;
    for (const it of m.items) {
      if (it.col) {
        const sx = cx + IND / 2; let yy = cy;
        for (const k of it.col) {
          const hk = hOf(k);
          P.set(k, { x: cx + IND, y: yy, w: W, h: hk });
          edges.push({ k, d: `M${px},${py}V${busY}H${sx}V${yy + hk / 2}H${cx + IND}` });
          M.set(k, { w: W, h: hk });
          yy += hk + SG;
        }
      } else {
        const bm = M.get(it.b), bx = cx + (bm.w - W) / 2 + W / 2;
        edges.push({ k: it.b, d: `M${px},${py}V${busY}H${bx}V${cy}` });
        place(it.b, cx, cy);
      }
      cx += it.w + GX;
    }
  }
  function layout() {
    M = new Map(); P = new Map(); edges = [];
    const m = measure(ROOT); size = { w: m.w, h: m.h };
    place(ROOT, 0, 0);
  }

  // ---------- view transform
  let k = 1, tx = 0, ty = 0;
  const apply = () => { world.style.transform = `translate(${tx}px,${ty}px) scale(${k})`; };
  function visRect() {
    let x0 = 0, x1 = innerWidth, y0 = 0, y1 = innerHeight;
    const rail = $("rail").getBoundingClientRect(), dr = $("drawer"), b = bar.getBoundingClientRect();
    if (innerWidth > 760) { x0 = rail.right; if (!dr.hidden) x1 = dr.getBoundingClientRect().left; }
    else { y0 = rail.bottom; if (!dr.hidden) y1 = Math.min(y1, dr.getBoundingClientRect().top); }
    if (b.height) y1 = Math.min(y1, b.top);
    return { x: x0, y: y0, w: Math.max(120, x1 - x0), h: Math.max(120, y1 - y0) };
  }
  let tween = null;
  function moveTo(nk, ntx, nty, ms = 380) {
    cancelAnimationFrame(tween);
    if (reduced || !ms) { k = nk; tx = ntx; ty = nty; apply(); return; }
    const a = { k, tx, ty }, t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - t, 3);
      k = a.k + (nk - a.k) * e; tx = a.tx + (ntx - a.tx) * e; ty = a.ty + (nty - a.ty) * e; apply();
      if (t < 1) tween = requestAnimationFrame(step);
    };
    tween = requestAnimationFrame(step);
  }
  function fit(ms) {
    const r = visRect(), pad = 28;
    const nk = Math.max(0.12, Math.min(1, (r.w - pad * 2) / size.w, (r.h - pad * 2) / size.h));
    moveTo(nk, r.x + (r.w - size.w * nk) / 2, r.y + pad, ms);
  }
  function centerOn(id, ms) {
    const p = P.get(id); if (!p) return;
    const r = visRect(), nk = Math.max(k, 0.8);
    moveTo(nk, r.x + r.w / 2 - (p.x + p.w / 2) * nk, r.y + r.h / 2 - (p.y + p.h / 2) * nk, ms);
  }
  function zoomAt(cx, cy, f) {
    const nk = Math.max(0.12, Math.min(2.2, k * f));
    tx = cx - (cx - tx) * (nk / k); ty = cy - (cy - ty) * (nk / k); k = nk; apply();
  }

  // ---------- calls of the selection, drawn to whatever box holds the other end
  function shownBox(id) { while (id && !P.has(id)) id = par.get(id); return id; }
  function under(id) {
    const out = new Set(), st = [id];
    while (st.length) { const x = st.pop(); if (get(x).t === "sym") out.add(x); for (const c of kids.get(x) || []) st.push(c); }
    return out;
  }
  function partners(sel) {
    const res = { out: new Map(), in: new Map() };
    if (!sel || !P.has(sel) || sel === ROOT) return res;
    const D = under(sel);
    const add = (m, t) => {
      const n = byId.get(t); if (!n || D.has(t) || !shown(n)) return;
      const b = shownBox(t); if (!b || b === sel || b === ROOT) return;
      m.set(b, (m.get(b) || 0) + 1);
    };
    for (const s of D) { for (const t of callOut.get(s) || []) add(res.out, t); for (const t of callIn.get(s) || []) add(res.in, t); }
    return res;
  }
  function curve(a, b, dir) {
    const ac = { x: a.x + a.w / 2, y: a.y + a.h / 2 }, bc = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
    const dx = bc.x - ac.x, dy = bc.y - ac.y;
    let s, e, c1, c2;
    if (Math.abs(dx) > Math.abs(dy) * 1.2 || Math.abs(dx) > W) {
      const sg = Math.sign(dx) || 1, off = Math.max(40, Math.abs(dx) * 0.35);
      s = { x: ac.x + sg * a.w / 2, y: ac.y }; e = { x: bc.x - sg * b.w / 2, y: bc.y };
      c1 = { x: s.x + sg * off, y: s.y }; c2 = { x: e.x - sg * off, y: e.y };
    } else {
      const sg = Math.sign(dy) || 1, off = Math.max(30, Math.abs(dy) * 0.35);
      s = { x: ac.x + 30 * (dir === "in" ? -1 : 1), y: ac.y + sg * a.h / 2 }; e = { x: bc.x + 30 * (dir === "in" ? -1 : 1), y: bc.y - sg * b.h / 2 };
      c1 = { x: s.x, y: s.y + sg * off }; c2 = { x: e.x, y: e.y - sg * off };
    }
    return dir === "in" ? `M${e.x},${e.y}C${c2.x},${c2.y} ${c1.x},${c1.y} ${s.x},${s.y}` : `M${s.x},${s.y}C${c1.x},${c1.y} ${c2.x},${c2.y} ${e.x},${e.y}`;
  }

  // ---------- render
  const LANG = { typescript: "ts", javascript: "js", python: "py", markdown: "md", powershell: "ps1" };
  function ensureVisible(id, minK = 0) {
    const p = P.get(id); if (!p) return;
    const nk = Math.max(k, minK), ntx = (p.x + p.w / 2) * (k - nk) + tx, nty = (p.y + p.h / 2) * (k - nk) + ty;   // zoom in about the box
    let x0 = p.x, y0 = p.y, x1 = p.x + p.w, y1 = p.y + p.h;
    const st = [id];
    while (st.length) { const x = st.pop(); if (!open.has(x)) continue;
      for (const c of disp(x)) { const q = P.get(c); if (!q) continue; x0 = Math.min(x0, q.x); y0 = Math.min(y0, q.y); x1 = Math.max(x1, q.x + q.w); y1 = Math.max(y1, q.y + q.h); st.push(c); } }
    const r = visRect(), m = 20, L = r.x + m, T = r.y + m, R = r.x + r.w - m, B = r.y + r.h - m;
    const fitAxis = (a0, a1, b0, b1, lo, hi) => {     // whole subtree if it fits, else the box itself
      const [s0, s1] = a1 - a0 <= hi - lo ? [a0, a1] : [b0, b1];
      return s0 < lo ? lo - s0 : s1 > hi ? hi - s1 : 0;
    };
    const dx = fitAxis(x0 * nk + ntx, x1 * nk + ntx, p.x * nk + ntx, (p.x + p.w) * nk + ntx, L, R);
    const dy = fitAxis(y0 * nk + nty, y1 * nk + nty, p.y * nk + nty, (p.y + p.h) * nk + nty, T, B);
    if (dx || dy || nk !== k) moveTo(nk, ntx + dx, nty + dy, 320);
  }
  function meta(n, id) {
    const c = cnt.get(id);
    if (n.t === "sym") return (n.kind === "function" || n.kind === "method" ? "" : (n.kind || "") + " ") + (n.line ? ":" + n.line : "");
    if (n.t === "file") return [LANG[n.lang] || n.lang, n.lines ? n.lines.toLocaleString() + " lines" : "", c.s ? c.s + " fn" : "", n.test ? "test" : ""].filter(Boolean).join(" · ");
    return `${c.f.toLocaleString()} file${c.f === 1 ? "" : "s"} · ${c.s.toLocaleString()} fn`;
  }
  function render(anchor) {
    const before = anchor && P.get(anchor) ? { x: P.get(anchor).x * k + tx, y: P.get(anchor).y * k + ty } : null;
    layout();
    if (before && P.get(anchor)) { tx = before.x - P.get(anchor).x * k; ty = before.y - P.get(anchor).y * k; apply(); }
    const sel = state.sel && P.has(state.sel) ? state.sel : null;
    const onPath = new Set(); for (let x = sel; x; x = par.get(x)) onPath.add(x);
    const pr = partners(sel);
    eSvg.setAttribute("width", size.w); eSvg.setAttribute("height", size.h);
    cSvg.setAttribute("width", size.w); cSvg.setAttribute("height", size.h);
    const es = edges.map((e) => ({ hot: onPath.has(e.k), d: e.d })).sort((a, b) => a.hot - b.hot);
    eSvg.innerHTML = es.map((e) => `<path class="e${e.hot ? " hot" : ""}" d="${e.d}"/>`).join("");
    const sp = sel && P.get(sel), cs = [];
    const wOf = (n) => 1.3 + Math.min(3, Math.log2(n));
    for (const [b, n] of pr.out) cs.push(`<path class="c out" stroke-width="${wOf(n)}" marker-end="url(#tmo)" d="${curve(sp, P.get(b), "out")}"/>`);
    for (const [b, n] of pr.in) cs.push(`<path class="c in" stroke-width="${wOf(n)}" marker-end="url(#tmi)" d="${curve(sp, P.get(b), "in")}"/>`);
    cSvg.innerHTML = `<defs><marker id="tmo" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0L8,4L0,8z" fill="${ACCENT}"/></marker>
      <marker id="tmi" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0L8,4L0,8z" fill="#8fb3ff"/></marker></defs>` + cs.join("");
    pane.classList.toggle("focus", !!(sel && (pr.out.size || pr.in.size)));
    let h = "";
    for (const [id, p] of P) {
      const n = get(id), dk = disp(id).length, isOpen = open.has(id) && dk;
      const color = n.t === "root" ? ACCENT : AREA[n.area] || "#9aa7bb";
      const cls = ["tn", "t-" + n.t, n.test ? "test" : "", n.unused ? "unused" : "", id === sel ? "sel" : "", onPath.has(id) ? "on-path" : "",
        pr.out.has(id) ? "p-out" : "", pr.in.has(id) ? "p-in" : ""].filter(Boolean).join(" ");
      const name = n.t === "sym" && n.parent && get(par.get(id)).t !== "sym" ? n.parent + "." + n.name : n.name;
      const badge = pr.out.has(id) ? `<span class="tn-badge out">→${pr.out.get(id)}</span>` : pr.in.has(id) ? `<span class="tn-badge in">←${pr.in.get(id)}</span>` : "";
      h += `<button class="${cls}" data-id="${esc(id)}" style="left:${p.x}px;top:${p.y}px;width:${p.w}px;height:${p.h}px;--c:${color}"` +
        (dk ? ` aria-expanded="${isOpen ? "true" : "false"}"` : "") + ` title="${esc(n.path && n.t !== "file" ? n.path + (n.line ? ":" + n.line : "") : n.path || n.name)}">` +
        `<span class="tn-name">${esc(name)}</span><span class="tn-meta">${esc(meta(n, id))}</span>` +
        (dk ? `<span class="tn-tog" data-tog>${isOpen ? "−" : "+" + dk}</span>` : "") + badge + `</button>`;
    }
    nBox.innerHTML = h;
    const hint = $("treeHint");
    hint.innerHTML = sel && (pr.out.size || pr.in.size)
      ? `<span class="key"><i></i>calls</span> &nbsp;<span class="key"><i class="in"></i>called by</span>`
      : `<b>${P.size.toLocaleString()}</b> boxes · click to open · drag to move · scroll to zoom`;
  }

  function reveal(id) {
    if (!get(id) || !par.has(id)) return false;
    for (let x = par.get(id); x; x = par.get(x)) open.add(x);
    render(); centerOn(id); return true;
  }

  // ---------- input: click opens and selects, the pill only folds, drag pans, wheel and pinch zoom
  let quiet = false;
  nBox.addEventListener("click", (e) => {
    if (moved) return;
    const b = e.target.closest(".tn"); if (!b) return;
    const id = b.dataset.id, has = disp(id).length;
    if (e.target.closest("[data-tog]")) { open.has(id) ? open.delete(id) : open.add(id); render(id); ensureVisible(id); return; }
    if (has && (!open.has(id) || state.sel === id)) open.has(id) && state.sel === id ? open.delete(id) : open.add(id);
    quiet = true;
    if (id === ROOT) { select(null); render(id); } else { select(id); render(id); }
    ensureVisible(id, get(id).t === "file" || get(id).t === "sym" ? 0.8 : 0);
    quiet = false;
  });
  const ptrs = new Map(); let moved = false, start = null, pinch = null;
  pane.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY }); moved = false; cancelAnimationFrame(tween);
    start = { x: e.clientX, y: e.clientY, tx, ty };
    if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k }; }
  });
  pane.addEventListener("pointermove", (e) => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (ptrs.size === 2 && pinch) {
      const [a, b] = [...ptrs.values()], d = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, (pinch.k * d / pinch.d) / k); moved = true; return;
    }
    if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 5) return;
    if (!moved) { moved = true; pane.classList.add("dragging"); try { pane.setPointerCapture(e.pointerId); } catch (_) {} }
    tx = start.tx + e.clientX - start.x; ty = start.ty + e.clientY - start.y; apply();
  });
  const endPtr = (e) => {
    ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null;
    if (!ptrs.size) { pane.classList.remove("dragging"); setTimeout(() => { moved = false; }, 0); }
    if (ptrs.size === 1) { const [p] = [...ptrs.values()]; start = { x: p.x, y: p.y, tx, ty }; }
  };
  pane.addEventListener("pointerup", endPtr); pane.addEventListener("pointercancel", endPtr);
  pane.addEventListener("click", (e) => { if (!moved && !e.target.closest(".tn") && state.sel) { quiet = true; select(null); render(); quiet = false; } });
  pane.addEventListener("wheel", (e) => {
    e.preventDefault(); cancelAnimationFrame(tween);
    const d = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    zoomAt(e.clientX, e.clientY, Math.exp(-d * (e.ctrlKey ? 0.01 : 0.0016)));
  }, { passive: false });
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("[data-t]"); if (!b) return;
    const r = visRect(), t = b.dataset.t;
    if (t === "fit") fit();
    else if (t === "in" || t === "out") { const f = t === "in" ? 1.25 : 0.8; const nk = Math.max(0.12, Math.min(2.2, k * f));
      const cx = r.x + r.w / 2, cy = r.y + r.h / 2; moveTo(nk, cx - (cx - tx) * (nk / k), cy - (cy - ty) * (nk / k), 200); }
    else if (t === "files") { for (const [id] of kids) { const n = get(id); if (n.t === "root" || n.t === "area" || n.t === "comp") open.add(id); } render(); fit(); }
    else if (t === "areas") { open.clear(); open.add(ROOT); for (const a of kids.get(ROOT) || []) open.add(a); render(); fit(); }
  });

  // ---------- plug into the engine
  const pause3d = () => {
    if (typeof Graph !== "undefined" && Graph.pauseAnimation) Graph.pauseAnimation();
    else if (typeof renderer !== "undefined" && !renderer._treeRender) { renderer._treeRender = renderer.render; renderer.render = () => {}; }
  };
  const resume3d = () => {
    if (typeof Graph !== "undefined" && Graph.resumeAnimation) Graph.resumeAnimation();
    else if (typeof renderer !== "undefined" && renderer._treeRender) { renderer.render = renderer._treeRender; delete renderer._treeRender; }
  };
  document.addEventListener("visibilitychange", () => { if (state.view === "tree" && !document.hidden) pause3d(); });
  const press = (v) => { for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === v)); };
  const NOTE = "Tree: the repo top down. Click a box to open it and read it in the drawer; the selected box draws its calls (gold out, blue in) to wherever the other end sits.";

  const engineSelect = select;
  select = function (id, fly) {
    engineSelect(id, state.view === "tree" ? false : fly);
    if (state.view !== "tree" || quiet) return;
    if (!id || !reveal(id)) render();
  };
  const engineSetView = setView;
  let prev = null, first = true;
  setView = function (v) {
    if (v === "tree") {
      if (state.view === "tree") return;
      prev = state.view;
      document.body.classList.remove("linear");
      state.view = "tree";
      document.body.classList.add("tree");
      pause3d(); press("tree");
      const tip = $("tip"); if (tip) tip.hidden = true;
      $("viewNote").textContent = NOTE;
      try { history.replaceState(null, "", "#tree"); } catch (e) {}
      if (state.sel && !P.has(state.sel) && par.has(state.sel)) for (let x = par.get(state.sel); x; x = par.get(x)) open.add(x);
      render();
      if (first) { first = false; fit(0); } else if (state.sel && P.has(state.sel)) centerOn(state.sel, 0);
      return;
    }
    if (state.view !== "tree") return engineSetView(v);
    document.body.classList.remove("tree");
    resume3d();
    state.view = prev;
    if (prev === "linear") document.body.classList.add("linear");
    try { history.replaceState(null, "", prev === "linear" ? "#linear" : location.pathname + location.search); } catch (e) {}
    if (v === prev && v !== "explore") {
      press(v);
      if (typeof NOTES !== "undefined" && NOTES[v]) $("viewNote").textContent = NOTES[v];
    } else engineSetView(v);
    if (typeof resize === "function") resize();
  };

  // filters in the rail re-draw the tree
  const later = () => { if (state.view === "tree") setTimeout(() => render(), 0); };
  $("rail").addEventListener("change", later);
  const lg = $("legend"); if (lg) lg.addEventListener("click", later);
  addEventListener("resize", () => { if (state.view === "tree") apply(); });
  addEventListener("keydown", (e) => {
    if (state.view !== "tree" || e.target.closest("input, textarea")) return;
    if (e.key === "Escape" && state.sel && !(document.getElementById("code") && !document.getElementById("code").hidden)) { quiet = true; select(null); render(); quiet = false; }
    if (e.key === "0" && !e.metaKey && !e.ctrlKey) fit();
  });

  if ((typeof ATLAS_START_HASH === "string" ? ATLAS_START_HASH : location.hash) === "#tree") setView("tree");
})();
