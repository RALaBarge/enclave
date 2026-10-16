// Chat: one session at a time. History comes from get_messages, then the SSE feed adds live updates.
// Pi event shapes are read defensively: text may sit in message.content blocks, in a delta, or in a plain `text`.
export const spec = { id: "chat", title: "Chat", order: 2, allow: ["/sessions"] };

export const css = `
.log{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.msg{border:1px solid var(--bd);border-radius:8px;padding:8px 10px;background:var(--card);white-space:pre-wrap;word-break:break-word}
.msg.user{background:var(--bg)}
.who{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:2px}
.ev{font-size:12px;color:var(--mut);font-family:ui-monospace,Menlo,Consolas,monospace}
.row{display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:6px 8px;min-width:0}
button{font:inherit;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:5px;padding:6px 12px;cursor:pointer}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}
.mut{color:var(--mut)}.bad{color:var(--bad)}
`;

const MAX_ITEMS = 500;
let sid = null, items = [], live = null, busy = false, err = "", sent = 0;

const textOf = (m) => !m ? "" : typeof m.text === "string" ? m.text : typeof m.content === "string" ? m.content
  : Array.isArray(m.content) ? m.content.map((b) => (typeof b === "string" ? b : b && b.type === "text" ? b.text : "")).join("") : "";

const push = (it) => { items.push(it); if (items.length > MAX_ITEMS) items = items.slice(-MAX_ITEMS); return it; };
const note = (text) => push({ role: "event", text });

function onEvent(ev) {
  const t = ev.type, d = ev.assistantMessageEvent;
  if (t === "agent_start") { busy = true; live = null; }
  else if (t === "agent_settled" || t === "agent_end") { busy = false; live = null; }
  else if (t === "message_update") {
    const delta = d && typeof d.delta === "string" ? d.delta : null, full = textOf(ev.message) || textOf(ev);
    if (delta === null && !full) return;
    live ||= push({ role: "assistant", text: "" });
    live.text = delta === null ? full : live.text + delta;
  } else if (t === "tool_execution_start") note("tool: " + (ev.toolName || ev.name || "?"));
  else if (t === "extension_ui_request") note("extension asks: " + (ev.title || ev.method || "?"));
}

const bubble = (it) => it.role === "event" ? ["div", { c: "ev" }, it.text]
  : ["div", { c: "msg " + (it.role === "user" ? "user" : "") }, ["div", { c: "who" }, it.role], it.text];

// `k` is a key: when it changes the engine builds a fresh element, which is how the box is cleared after a send.
const view = () => !sid ? ["div", { c: "mut" }, "Open a session from the Sessions tab."] : ["div", {},
  ["div", { c: "log" }, ...items.map(bubble), busy ? ["div", { c: "mut" }, "working..."] : ""],
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
    sid = m.session; items = []; live = null; busy = false; err = "";
    return { streams: [...prev, { id: "events", path: "/sessions/" + sid + "/events" }], calls: [cmd("history", { type: "get_messages" })], view: view() };
  }
  if (m.type === "result") {
    if (m.id === "history" && m.status === 200 && m.body.success) {
      items = (m.body.data.messages || []).map((x) => ({ role: x.role || "?", text: textOf(x) })).filter((x) => x.text);
    } else if (m.status >= 400 || (m.body && m.body.success === false)) err = (m.body && m.body.error) || "request failed";
    return { view: view() };
  }
  if (m.type === "event" && m.stream === "events") { onEvent(m.data); return { view: view() }; }
  if (m.type !== "ui" || !sid) return {};
  if (m.name === "abort") return { calls: [cmd("abort", { type: "abort" })] };
  const text = (m.values.prompt || "").trim();
  if (m.name !== "send" || !text) return {};
  push({ role: "user", text }); live = null; busy = true; err = ""; sent++;
  return { view: view(), calls: [cmd("send", { type: "prompt", message: text })] };
}
