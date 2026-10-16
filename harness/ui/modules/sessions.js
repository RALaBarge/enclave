// Sessions: list, create, open (hands the session to the chat module), remove.
export const spec = { id: "sessions", title: "Sessions", order: 1, allow: ["/sessions"] };

export const css = `
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:5px 7px;min-width:0}
button{font:inherit;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:5px;padding:5px 10px;cursor:pointer}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}
button.d{color:var(--bad)}
table{width:100%;border-collapse:collapse}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);text-align:left;padding:4px 6px}
td{padding:6px;border-top:1px solid var(--bd);word-break:break-word}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.mut{color:var(--mut)}.bad{color:var(--bad)}.ok{color:var(--ok)}
`;

let sessions = [], err = "";

const row = (s) => ["tr", {},
  ["td", {}, s.name || ["span", { c: "mut" }, "(unnamed)"]],
  ["td", { c: "mono" }, s.cwd],
  ["td", { c: s.alive ? "ok" : "bad" }, s.alive ? "running" : "exited " + s.exit_code],
  ["td", {}, ["button", { c: "p", on: "open", d: s.id, disabled: !s.alive }, "Open"], " ",
    ["button", { c: "d", on: "remove", d: s.id }, "Remove"]]];

const view = () => ["div", {},
  ["div", { c: "row" },
    ["input", { name: "cwd", placeholder: "/path/to/project", size: 32 }],
    ["input", { name: "name", placeholder: "name (optional)", size: 18 }],
    ["button", { c: "p", on: "create" }, "New session"],
    ["button", { on: "refresh" }, "Refresh"]],
  err ? ["div", { c: "bad" }, err] : "",
  sessions.length
    ? ["table", {}, ["thead", {}, ["tr", {}, ["th", {}, "Name"], ["th", {}, "Directory"], ["th", {}, "State"], ["th", {}, ""]]],
        ["tbody", {}, ...sessions.map(row)]]
    : ["div", { c: "mut" }, "No sessions."]];

const list = { calls: [{ id: "list", method: "GET", path: "/sessions" }] };
const fail = (m) => (m.body && m.body.error) || "request failed (" + m.status + ")";

export function update(m) {
  if (m.type === "init") return { ...list, view: view() };
  if (m.type === "result") {
    if (m.id === "list" && m.status === 200) sessions = m.body;
    err = m.status >= 400 ? fail(m) : "";
    return m.id === "list" || err ? { view: view() } : list;
  }
  if (m.type !== "ui") return {};
  if (m.name === "refresh") return list;
  if (m.name === "open") return { emit: ["chat", "tree", "inspect"].map((to) => ({ to, msg: { type: "open", session: m.d } })), focus: "chat" };
  if (m.name === "remove") return { calls: [{ id: "remove", method: "DELETE", path: "/sessions/" + m.d }] };
  if (m.name === "create") {
    const cwd = (m.values.cwd || "").trim(), name = (m.values.name || "").trim();
    if (!cwd) { err = "enter a directory"; return { view: view() }; }
    return { calls: [{ id: "create", method: "POST", path: "/sessions", body: name ? { cwd, name } : { cwd } }] };
  }
  return {};
}
