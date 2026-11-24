// Chat: one session, drawn as turns. A turn is a prompt and everything Pi does until the next one: text and thinking
// in the main column, each tool call as a card in the side column with its request and result, numbered like the
// marker where the model made the call. Opening a session reads its last event number, loads get_messages, then
// streams the events after that number, so nothing is drawn twice. Deltas draw the live message; its message_end replaces
// them with the message as Pi settled it.
export const spec = { id: "chat", title: "Chat", order: 2, allow: ["/sessions"] };

export const css = `
.log{display:flex;flex-direction:column;gap:14px;margin-bottom:12px}
.turn{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:12px;align-items:start}
.turn.notools{grid-template-columns:minmax(0,1fr)}
@media (max-width:760px){.turn{grid-template-columns:minmax(0,1fr)}}
.main,.side{display:flex;flex-direction:column;gap:6px;min-width:0}
.msg{border:1px solid var(--bd);border-radius:8px;padding:8px 10px;background:var(--card);white-space:pre-wrap;word-break:break-word}
.msg.user{background:var(--bg);border-color:var(--ac)}
.who{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:2px}
.think{border:1px dashed #8a6fbf;border-radius:16px 16px 16px 3px;padding:6px 12px;background:rgba(138,111,191,.10);
  color:var(--mut);font-style:italic;white-space:pre-wrap;word-break:break-word}
.think>summary{font-style:normal;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8a6fbf;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:12px;font-family:ui-monospace,Menlo,Consolas,monospace;border:1px solid var(--bd);
  border-radius:10px;padding:1px 8px;color:var(--mut)}
.tool{border:1px solid var(--bd);border-left:3px solid var(--mut);border-radius:6px;background:var(--card);padding:4px 8px;font-size:12px}
.tool.ok{border-left-color:var(--ok)}.tool.error{border-left-color:var(--bad)}.tool.running{border-left-color:var(--ac)}
.tool>summary{cursor:pointer;font-family:ui-monospace,Menlo,Consolas,monospace}
.tool .st{color:var(--mut)}
.tool pre{margin:2px 0 6px;max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:var(--bg);
  border-radius:4px;padding:4px 6px}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-top:4px}
.ev{font-size:12px;color:var(--mut);font-family:ui-monospace,Menlo,Consolas,monospace}
.row{display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:6px 8px;min-width:0}
button{font:inherit;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:5px;padding:6px 12px;cursor:pointer}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}
.mut{color:var(--mut)}.bad{color:var(--bad)}
`;

const MAX_TURNS = 200, MAX_RESULT = 20000;
let sid = null, seq = 0, turns = [], cur = null, busy = false, err = "", sent = 0;

const blocks = (m) => !m ? [] : typeof m.content === "string" ? [{ type: "text", text: m.content }]
  : Array.isArray(m.content) ? m.content : typeof m.text === "string" ? [{ type: "text", text: m.text }] : [];
const textOf = (m) => blocks(m).map((b) => (typeof b === "string" ? b : b && b.type === "text" ? b.text : "")).join("");
const json = (v) => v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2);

function newTurn(user) {
  const t = { user, pending: false, parts: [], tools: [] };
  turns.push(t);
  if (turns.length > MAX_TURNS) turns = turns.slice(-MAX_TURNS);
  return t;
}
const last = () => turns[turns.length - 1] || newTurn(null);

// A card belongs to the turn that made the call; results and execution events find it by id, newest turn first.
function findTool(id) {
  for (let i = turns.length - 1; i >= 0; i--) { const c = turns[i].tools.find((x) => x.id === id); if (c) return c; }
  return null;
}
function tool(id, name) {
  const c = findTool(id);
  if (c) { if (name) c.name = name; return c; }
  const t = last(), n = { id, name: name || "?", n: t.tools.length + 1, args: undefined, status: "called", ms: null, result: "" };
  t.tools.push(n);
  return n;
}

function addAssistant(t, m) {
  for (const b of blocks(m)) {
    if (typeof b === "string") { if (b) t.parts.push({ k: "text", text: b }); }
    else if (b.type === "text") { if (b.text) t.parts.push({ k: "text", text: b.text }); }
    else if (b.type === "thinking") t.parts.push({ k: "thinking", text: b.thinking || "(not shown by the provider)" });
    else if (b.type === "toolCall") { tool(b.id, b.name).args = b.arguments; t.parts.push({ k: "tool", id: b.id }); }
  }
}
function setResult(m) {
  const c = tool(m.toolCallId, m.toolName);
  c.result = textOf(m).slice(0, MAX_RESULT);
  c.status = m.isError ? "error" : "ok";
}
function addMessage(m) {
  if (m.role === "user") newTurn(textOf(m));
  else if (m.role === "assistant") addAssistant(last(), m);
  else if (m.role === "toolResult") setResult(m);
}

// Live deltas: contentIndex says which block of the current message a delta belongs to.
function delta(d) {
  if (!cur) { const t = last(); cur = { turn: t, start: t.parts.length, idx: {} }; }
  const i = d.contentIndex ?? 0, kind = d.type && d.type.startsWith("thinking") ? "thinking" : "text";
  if (d.type === "toolcall_end" && d.toolCall) {
    const c = tool(d.toolCall.id, d.toolCall.name);
    c.args = d.toolCall.arguments;
    cur.turn.parts.push({ k: "tool", id: c.id });
    return;
  }
  if (typeof d.delta !== "string" || (d.type && !/^(text|thinking)_delta$/.test(d.type))) return;
  cur.idx[i] ||= cur.turn.parts[cur.turn.parts.push({ k: kind, text: "" }) - 1];
  cur.idx[i].text += d.delta;
}

function onEvent(ev) {
  const t = ev.type, m = ev.message;
  if (t === "agent_start") busy = true;
  else if (t === "agent_settled" || t === "agent_end") { busy = false; cur = null; }
  else if (t === "message_start" && m && m.role === "assistant") { const tr = last(); cur = { turn: tr, start: tr.parts.length, idx: {} }; }
  else if (t === "message_update" && ev.assistantMessageEvent) delta(ev.assistantMessageEvent);
  else if (t === "message_end" && m) {
    if (m.role === "user") {
      const tr = turns[turns.length - 1], x = textOf(m);
      if (tr && tr.pending && tr.user === x) tr.pending = false; else newTurn(x);
    } else if (m.role === "assistant") {
      const tr = cur ? cur.turn : last();
      tr.parts.splice(cur ? cur.start : tr.parts.length);
      addAssistant(tr, m);
      cur = null;
    } else if (m.role === "toolResult") setResult(m);
  } else if (t === "tool_execution_start") {
    const c = tool(ev.toolCallId, ev.toolName);
    if (ev.args !== undefined) c.args = ev.args;
    c.status = "running";
  } else if (t === "tool_execution_end") {
    const c = tool(ev.toolCallId, ev.toolName), r = textOf(ev.result);
    c.status = ev.isError ? "error" : "ok";
    c.ms = typeof ev.durationMs === "number" ? ev.durationMs : null;
    if (r) c.result = r.slice(0, MAX_RESULT);
  } else if (t === "extension_ui_request") last().parts.push({ k: "note", text: "extension asks: " + (ev.title || ev.method || "?") });
}

// details elements carry `open` only when built, so a box the user opens or closes stays that way across updates.
function part(t, p) {
  if (p.k === "thinking") return ["details", { c: "think", open: true }, ["summary", {}, "thinking"], p.text];
  if (p.k === "note") return ["div", { c: "ev" }, p.text];
  if (p.k === "tool") {
    const c = t.tools.find((x) => x.id === p.id);
    return c ? ["span", { c: "chip" }, "[" + c.n + "] " + c.name] : "";
  }
  return ["div", { c: "msg" }, ["div", { c: "who" }, "assistant"], p.text];
}
// Markers of calls made together share one line.
function chips(t) {
  const out = [];
  for (const p of t.parts) {
    const v = part(t, p), prev = out[out.length - 1];
    if (p.k === "tool" && Array.isArray(prev) && prev[1].c === "chips") prev.push(v);
    else out.push(p.k === "tool" ? ["div", { c: "chips" }, v] : v);
  }
  return out;
}
const card = (c) => ["details", { c: "tool " + c.status },
  ["summary", {}, "[" + c.n + "] " + c.name + " ", ["span", { c: "st" }, c.status + (c.ms !== null ? " " + (c.ms / 1000).toFixed(1) + "s" : "")]],
  ["div", { c: "lbl" }, "request"], ["pre", {}, json(c.args) || "(none)"],
  ["div", { c: "lbl" }, "result"], ["pre", {}, c.result || (c.status === "running" ? "..." : "(none)")]];
const turnView = (t) => ["div", { c: "turn" + (t.tools.length ? "" : " notools") },
  ["div", { c: "main" },
    t.user !== null ? ["div", { c: "msg user" }, ["div", { c: "who" }, "you"], t.user] : "",
    ...chips(t)],
  t.tools.length ? ["div", { c: "side" }, ...t.tools.map(card)] : ""];

// `k` is a key: when it changes the engine builds a fresh element, which is how the box is cleared after a send.
const view = () => !sid ? ["div", { c: "mut" }, "Open a session from the Sessions tab."] : ["div", {},
  ["div", { c: "log" }, ...turns.map(turnView), busy ? ["div", { c: "mut" }, "working..."] : ""],
  err ? ["div", { c: "bad" }, err] : "",
  ["div", { c: "row" },
    ["textarea", { name: "prompt", rows: 3, placeholder: "Message (Enter sends, Shift+Enter for a new line)", k: sent, on: { enter: "send" } }],
    ["button", { c: "p", on: "send" }, "Send"],
    ["button", { on: "abort", disabled: !busy }, "Stop"]]];

const cmd = (id, body) => ({ id, method: "POST", path: "/sessions/" + sid + "/command", body });

export function update(m) {
  if (m.type === "init") return { view: view() };
  if (m.type === "open") {
    const prev = sid ? [{ id: "events", close: true }] : [];
    sid = m.session; turns = []; cur = null; busy = false; err = "";
    return { streams: prev, calls: [{ id: "seq", method: "GET", path: "/sessions" }], view: view() };
  }
  if (m.type === "result") {
    if (m.id === "seq") {
      const s = m.status === 200 && Array.isArray(m.body) ? m.body.find((x) => x.id === sid) : null;
      if (!s) { err = "session " + sid + " not found"; return { view: view() }; }
      seq = s.last_seq || 0;
      return { calls: [cmd("history", { type: "get_messages" })] };
    }
    const out = { view: view() };
    if (m.id === "history") {
      if (m.status === 200 && m.body && m.body.success) { turns = []; cur = null; for (const x of m.body.data.messages || []) addMessage(x); }
      out.streams = [{ id: "events", path: "/sessions/" + sid + "/events?since=" + seq }];
    }
    if (m.status >= 400 || (m.body && m.body.success === false)) err = (m.body && m.body.error) || "request failed";
    out.view = view();
    return out;
  }
  if (m.type === "event" && m.stream === "events") { onEvent(m.data); return { view: view() }; }
  if (m.type !== "ui" || !sid) return {};
  if (m.name === "abort") return { calls: [cmd("abort", { type: "abort" })] };
  const text = (m.values.prompt || "").trim();
  if (m.name !== "send" || !text) return {};
  newTurn(text).pending = true; cur = null; busy = true; err = ""; sent++;
  return { view: view(), calls: [cmd("send", { type: "prompt", message: text })] };
}
