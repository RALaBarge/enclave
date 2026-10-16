// Directories: the project folders sessions run in, derived from GET /sessions (no bridge endpoint lists folders yet).
// Each has a live session to open, or starts a new one and hands it to the chat module.
export const spec = { id: "dirs", title: "Directories", order: 3, allow: ["/sessions"] };

export const css = `
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
button{font:inherit;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:5px;padding:5px 10px;cursor:pointer}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}
table{width:100%;border-collapse:collapse}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);text-align:left;padding:4px 6px}
td{padding:6px;border-top:1px solid var(--bd);word-break:break-word}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.mut{color:var(--mut)}.bad{color:var(--bad)}.ok{color:var(--ok)}
`;

let sessions = [], err = "";

// One entry per distinct cwd, with the id of a live session in it (if any).
const dirs = () => {
  const by = new Map();
  for (const s of sessions) {
    const d = by.get(s.cwd) || { cwd: s.cwd, live: null };
    if (s.alive) d.live = s.id;
    by.set(s.cwd, d);
  }
  return [...by.values()];
};

const row = (d) => ["tr", {},
  ["td", { c: "mono" }, d.cwd],
  ["td", { c: d.live ? "ok" : "mut" }, d.live ? "live" : "none"],
  ["td", {}, d.live ? ["button", { on: "open", d: d.live }, "Open live"] : "", " ",
    ["button", { c: "p", on: "start", d: d.cwd }, "New session"]]];

const view = () => {
  const ds = dirs();
  return ["div", {},
    ["div", { c: "row" }, ["button", { on: "refresh" }, "Refresh"]],
    err ? ["div", { c: "bad" }, err] : "",
    ds.length
      ? ["table", {}, ["thead", {}, ["tr", {}, ["th", {}, "Directory"], ["th", {}, "Live"], ["th", {}, ""]]],
          ["tbody", {}, ...ds.map(row)]]
      : ["div", { c: "mut" }, "No directories yet. Start a session from the Sessions tab."]];
};

const list = { calls: [{ id: "list", method: "GET", path: "/sessions" }] };
const fail = (m) => (m.body && m.body.error) || "request failed (" + m.status + ")";

export function update(m) {
  if (m.type === "init") return { ...list, view: view() };
  if (m.type === "result") {
    err = m.status >= 400 ? fail(m) : "";
    if (m.id === "list" && m.status === 200) sessions = m.body;
    if (m.id === "start" && m.status === 201) return { ...list, emit: [{ to: "chat", msg: { type: "open", session: m.body.id } }], focus: "chat" };
    return { view: view() };
  }
  if (m.type !== "ui") return {};
  if (m.name === "refresh") return list;
  if (m.name === "open") return { emit: [{ to: "chat", msg: { type: "open", session: m.d } }], focus: "chat" };
  if (m.name === "start") return { calls: [{ id: "start", method: "POST", path: "/sessions", body: { cwd: m.d } }] };
  return {};
}
