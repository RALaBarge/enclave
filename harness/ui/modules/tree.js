// Tree: the session's message tree from Pi's get_tree. Click a node to read it in full; Fork branches the session there.
// Assumed get_tree shape (not yet checked against real Pi): data is a list of entries {id, parentId, message|text|content}.
export const spec = { id: "tree", title: "Tree", order: 3, allow: ["/sessions"] };

export const css = `
.row{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.tree{border:1px solid var(--bd);border-radius:8px;background:var(--card);margin-bottom:12px;max-height:340px;overflow:auto;padding:4px 0}
.node{display:flex;gap:6px;align-items:baseline;padding:3px 10px;cursor:pointer}
.node:hover{background:var(--bg)}
.node.on{background:var(--ac);color:#fff}
.node.on .gl,.node.on .who,.node.on .br{color:#fff}
.gl{white-space:pre;color:var(--mut);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.who{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.txt{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.br{font-size:11px;color:var(--ac);white-space:nowrap}
.detail{border:1px solid var(--bd);border-radius:8px;background:var(--card);padding:10px}
.detail pre{white-space:pre-wrap;word-break:break-word;font:inherit;margin:6px 0 10px}
button{font:inherit;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:5px;padding:5px 10px;cursor:pointer}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}
.mut{color:var(--mut)}.bad{color:var(--bad)}
`;

let sid = null, rows = [], sel = null, err = "";

const msg = (e) => e.message || e;
const textOf = (m) => typeof m.content === "string" ? m.content
  : Array.isArray(m.content) ? m.content.map((b) => (typeof b === "string" ? b : b && b.type === "text" ? b.text : "")).join("")
  : typeof m.text === "string" ? m.text : "";

// Entries without text (model changes, labels) are skipped; a node's children attach to its nearest shown ancestor.
function build(list) {
  const byId = new Map(list.map((e) => [e.id, e]));
  const shown = new Map();
  for (const e of list) if (textOf(msg(e))) shown.set(e.id, { role: msg(e).role || "?", text: textOf(msg(e)) });
  const up = (p) => { while (p && !shown.has(p)) p = (byId.get(p) || {}).parentId; return p || null; };
  const kids = new Map();
  for (const e of list) if (shown.has(e.id)) {
    const p = up(e.parentId);
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(e.id);
  }
  const out = [];
  const walk = (id, depth) => {
    const c = kids.get(id) || [];
    out.push({ id, depth, n: c.length, ...shown.get(id) });
    for (const k of c) walk(k, depth + 1);
  };
  for (const r of kids.get(null) || []) walk(r, 0);
  return out;
}

const row = (r) => ["div", { c: "node" + (r.id === sel ? " on" : ""), on: "pick", d: r.id },
  ["span", { c: "gl" }, "  ".repeat(Math.min(r.depth, 20))],
  ["span", { c: "who" }, r.role],
  ["span", { c: "txt" }, r.text.replace(/\s+/g, " ").slice(0, 120)],
  r.n > 1 && ["span", { c: "br" }, "[" + r.n + " branches]"]];

const detail = (r) => !r ? ["div", { c: "mut" }, "Pick a node to read it in full."]
  : ["div", { c: "detail" }, ["div", { c: "who" }, r.role], ["pre", {}, r.text],
    ["button", { c: "p", on: "fork", d: r.id }, "Fork from here"], " ",
    ["span", { c: "mut" }, "Starts a new branch at this message."]];

const view = () => !sid ? ["div", { c: "mut" }, "Open a session from the Sessions tab."] : ["div", {},
  ["div", { c: "row" }, ["button", { on: "refresh" }, "Refresh"], ["span", { c: "mut" }, rows.length + " nodes"]],
  err ? ["div", { c: "bad" }, err] : "",
  ["div", { c: "tree" }, ...rows.map(row)],
  detail(rows.find((r) => r.id === sel))];

const cmd = (id, body) => ({ id, method: "POST", path: "/sessions/" + sid + "/command", body });
const load = () => ({ calls: [cmd("tree", { type: "get_tree" })] });

export function update(m) {
  if (m.type === "init") return { view: view() };
  if (m.type === "open") { sid = m.session; rows = []; sel = null; err = ""; return { ...load(), view: view() }; }
  if (m.type === "result") {
    const ok = m.status === 200 && m.body && m.body.success;
    err = ok ? "" : (m.body && m.body.error) || "request failed (" + m.status + ")";
    if (ok && m.id === "tree") rows = build(Array.isArray(m.body.data) ? m.body.data : (m.body.data || {}).entries || []);
    if (ok && m.id === "fork") return { ...load(), view: view(), emit: [{ to: "chat", msg: { type: "open", session: sid } }], focus: "chat" };
    return { view: view() };
  }
  if (m.type !== "ui" || !sid) return {};
  if (m.name === "refresh") return load();
  if (m.name === "pick") { sel = m.d; return { view: view() }; }
  if (m.name === "fork") return { calls: [cmd("fork", { type: "fork", entryId: m.d })] };
  return {};
}
