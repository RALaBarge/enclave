// Inspector: the raw Pi event feed of the open session as a timeline (filter by type, click a row for its JSON), and the
// user's prompts to replay. Replay forks the session at that prompt with Pi's `fork` and sends the prompt again.
export const spec = { id: "inspect", title: "Inspector", order: 3, allow: ["/sessions"] };

export const css = `
.row{display:flex;gap:8px;align-items:center;margin-bottom:10px}
select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:5px 7px}
button{font:inherit;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:5px;padding:4px 10px;cursor:pointer}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}
details{margin-bottom:12px}
.prompt{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--bd)}
.txt{white-space:pre-wrap;word-break:break-word}
.ev{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;border-top:1px solid var(--bd);padding:4px 2px;cursor:pointer;word-break:break-word}
.raw{margin:6px 0 2px;padding:8px;border:1px solid var(--bd);border-radius:6px;background:var(--card);white-space:pre-wrap;overflow-x:auto}
.mut{color:var(--mut)}.bad{color:var(--bad)}
`;

const MAX_ITEMS = 500;
let sid = null, rows = [], expanded = new Set(), filter = "", prompts = [], pending = "", busy = false, err = "";

const push = (r) => { rows.push(r); if (rows.length > MAX_ITEMS) rows = rows.slice(-MAX_ITEMS); };
const ok = (m) => m.status === 200 && m.body && m.body.success;
const fail = (m) => (m.body && m.body.error) || "request failed (" + m.status + ")";
const cmd = (id, body) => ({ id, method: "POST", path: "/sessions/" + sid + "/command", body });
const listPrompts = () => cmd("prompts", { type: "get_fork_messages" });

const row = (r) => ["div", { c: "ev", on: "toggle", d: String(r.seq) }, ["span", { c: "mut" }, r.time + " #" + r.seq + " "], ["b", {}, r.type],
  expanded.has(String(r.seq)) ? ["pre", { c: "raw" }, JSON.stringify(r.data, null, 2)] : ""];

// `busy` comes from the same agent_* events chat.js reads; replaying while a turn runs would interleave two prompts.
const view = () => !sid ? ["div", { c: "mut" }, "Open a session from the Sessions tab."] : ["div", {},
  ["details", {}, ["summary", {}, "Replay a prompt (" + prompts.length + ")"],
    ...prompts.map((p) => ["div", { c: "prompt" }, ["button", { c: "p", on: "replay", d: p.entryId, disabled: busy }, "Replay"], ["span", { c: "txt" }, p.text]])],
  ["div", { c: "row" },
    ["select", { name: "type", on: { change: "filter" } }, ["option", { value: "" }, "all types"],
      ...[...new Set(rows.map((r) => r.type))].map((t) => ["option", { value: t, selected: t === filter }, t])],
    ["span", { c: "mut" }, rows.length + " events"]],
  err ? ["div", { c: "bad" }, err] : "",
  ["div", {}, ...(filter ? rows.filter((r) => r.type === filter) : rows).map(row)]];

export function update(m) {
  if (m.type === "init") return { view: view() };
  if (m.type === "open") {
    const prev = sid ? [{ id: "events", close: true }] : [];
    sid = m.session; rows = []; expanded = new Set(); prompts = []; pending = ""; busy = false; err = "";
    return { streams: [...prev, { id: "events", path: "/sessions/" + sid + "/events" }], calls: [listPrompts()], view: view() };
  }
  if (m.type === "result") {
    if (m.id === "fork" && ok(m) && !(m.body.data || {}).cancelled) return { calls: [listPrompts(), cmd("replay", { type: "prompt", message: pending })], view: view() };
    if (m.id === "prompts" && ok(m)) prompts = (m.body.data.messages || []).filter((x) => x.entryId && x.text).map((x) => ({ entryId: x.entryId, text: x.text }));
    err = ok(m) ? "" : fail(m);
    return { view: view() };
  }
  if (m.type === "event" && m.stream === "events") {
    const t = m.data.type || "?";
    push({ seq: m.seq, type: t, time: new Date().toTimeString().slice(0, 8), data: m.data });
    if (t === "agent_start") busy = true;
    if (t === "agent_end" || t === "agent_settled") { busy = false; return { calls: [listPrompts()], view: view() }; }
    return { view: view() };
  }
  if (m.type !== "ui" || !sid) return {};
  if (m.name === "toggle") { expanded.has(m.d) ? expanded.delete(m.d) : expanded.add(m.d); return { view: view() }; }
  if (m.name === "filter") { filter = m.values.type || ""; return { view: view() }; }
  if (m.name === "replay") {
    const p = prompts.find((x) => x.entryId === m.d);
    if (!p || busy) return {};
    pending = p.text; err = "";
    return { calls: [cmd("fork", { type: "fork", entryId: p.entryId })], view: view() };
  }
  return {};
}
