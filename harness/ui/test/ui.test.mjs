// node --test harness/ui/test   (no DOM: modules are pure JSON in -> JSON out, so they run as they are)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validate } from "../view.js";
import { merge, runBatch } from "../host.js";

const files = JSON.parse(readFileSync(new URL("../modules/modules.json", import.meta.url)));
const load = (f) => import("../modules/" + f + "?" + Math.random());   // fresh module state per test

test("every module has a spec, css and emits only valid, JSON-clean views", async () => {
  for (const f of files) {
    const m = await load(f);
    assert.ok(m.spec.id && m.spec.title && Array.isArray(m.spec.allow), f);
    assert.equal(typeof m.css, "string");
    const out = m.update({ type: "init" });
    assert.deepEqual(validate(out.view), [], f);
    assert.deepEqual(JSON.parse(JSON.stringify(out)), out, f + " output is not plain JSON");
  }
});

test("validate rejects unsafe nodes", () => {
  assert.ok(validate(["script", {}, "x"]).length);
  assert.ok(validate(["div", { onclick: "x" }]).length);
  assert.ok(validate(["input", { type: "file" }]).length);
  assert.ok(validate(["div", { on: { mouseover: "x" } }]).length);
  assert.deepEqual(validate(["div", { c: "a", on: "go", d: "1" }, "text", ["span", {}, 3]]), []);
});

test("merge: last view wins, lists concatenate, a throwing module reports instead of dying", () => {
  const o = merge([{ view: 1, calls: [{ id: "a" }] }, { view: 2, calls: [{ id: "b" }], focus: "x" }, null, {}]);
  assert.deepEqual(o, { view: 2, calls: [{ id: "a" }, { id: "b" }], focus: "x" });
  const bad = { spec: { id: "m" }, update() { throw new Error("boom"); } };
  assert.match(runBatch(bad, [{}]).toast, /boom/);
});

test("sessions: lists, creates, opens into chat, removes, and surfaces errors", async () => {
  const s = await load("sessions.js");
  assert.equal(s.update({ type: "init" }).calls[0].path, "/sessions");
  let out = s.update({ type: "result", id: "list", status: 200, body: [{ id: "a1", name: "n", cwd: "/w", alive: true }] });
  assert.deepEqual(validate(out.view), []);
  assert.match(JSON.stringify(out.view), /"d":"a1"/);
  assert.equal(s.update({ type: "ui", name: "create", values: { cwd: " " } }).calls, undefined);
  assert.deepEqual(s.update({ type: "ui", name: "create", values: { cwd: "/w", name: "n" } }).calls[0].body, { cwd: "/w", name: "n" });
  assert.deepEqual(s.update({ type: "ui", name: "create", values: { cwd: "/w", name: "" } }).calls[0].body, { cwd: "/w" });
  out = s.update({ type: "ui", name: "open", d: "a1" });
  assert.deepEqual(out.emit.map((e) => [e.to, e.msg.session]), [["chat", "a1"], ["inspect", "a1"]]);
  assert.equal(out.focus, "chat");
  assert.equal(s.update({ type: "ui", name: "remove", d: "a1" }).calls[0].method, "DELETE");
  assert.match(JSON.stringify(s.update({ type: "result", id: "create", status: 400, body: { error: "cwd must be a dir" } }).view), /cwd must be a dir/);
  assert.equal(s.update({ type: "result", id: "remove", status: 200, body: {} }).calls[0].id, "list");   // success refreshes the list
});

test("chat: opens a stream, loads history, streams deltas into one bubble, sends, aborts", async () => {
  const c = await load("chat.js");
  const out = c.update({ type: "open", session: "ab12" });
  assert.deepEqual(out.streams, [{ id: "events", path: "/sessions/ab12/events" }]);
  assert.deepEqual(out.calls[0].body, { type: "get_messages" });
  c.update({ type: "result", id: "history", status: 200, body: { success: true, data: { messages: [{ role: "user", content: [{ type: "text", text: "hi" }] }] } } });
  const ev = (data) => c.update({ type: "event", stream: "events", data });
  ev({ type: "agent_start" });
  ev({ type: "message_update", assistantMessageEvent: { delta: "Hel" } });
  const v = JSON.stringify(ev({ type: "message_update", assistantMessageEvent: { delta: "lo" } }).view);
  assert.match(v, /Hello/);
  assert.equal(v.split("assistant").length - 1, 1, "deltas must extend one bubble");
  ev({ type: "tool_execution_start", toolName: "bash" });
  assert.match(JSON.stringify(ev({ type: "agent_settled" }).view), /tool: bash/);
  const send = c.update({ type: "ui", name: "send", values: { prompt: " go " } });
  assert.deepEqual(send.calls[0].body, { type: "prompt", message: "go" });
  assert.equal(c.update({ type: "ui", name: "send", values: { prompt: "" } }).calls, undefined);
  assert.equal(c.update({ type: "ui", name: "abort", values: {} }).calls[0].body.type, "abort");
  const reopen = c.update({ type: "open", session: "cd34" });
  assert.deepEqual(reopen.streams[0], { id: "events", close: true });
});

test("inspect: timeline of raw events, type filter, expand, replay through a fork", async () => {
  const s = await load("inspect.js");
  assert.deepEqual(validate(s.update({ type: "init" }).view), []);
  let out = s.update({ type: "open", session: "ab12" });
  assert.deepEqual(out.streams, [{ id: "events", path: "/sessions/ab12/events" }]);
  assert.deepEqual(out.calls[0], { id: "prompts", method: "POST", path: "/sessions/ab12/command", body: { type: "get_fork_messages" } });
  s.update({ type: "result", id: "prompts", status: 200, body: { success: true, data: { messages: [{ entryId: "e1", text: "first" }, { entryId: "e2", text: "second" }] } } });
  const ev = (seq, data) => s.update({ type: "event", stream: "events", seq, data });
  ev(1, { type: "agent_start" });
  out = ev(2, { type: "message_update", delta: "hi" });
  assert.match(JSON.stringify(out.view), / #2 /);
  assert.deepEqual(validate(out.view), []);
  out = s.update({ type: "ui", name: "filter", values: { type: "agent_start" } });
  assert.doesNotMatch(JSON.stringify(out.view), / #2 /);
  out = s.update({ type: "ui", name: "toggle", d: "1", values: {} });
  assert.match(JSON.stringify(out.view), /"c":"raw"/);
  assert.equal(s.update({ type: "ui", name: "replay", d: "e2", values: {} }).calls, undefined, "no replay while a turn runs");
  s.update({ type: "event", stream: "events", seq: 3, data: { type: "agent_settled" } });
  out = s.update({ type: "ui", name: "replay", d: "e2", values: {} });
  assert.deepEqual(out.calls[0].body, { type: "fork", entryId: "e2" });
  out = s.update({ type: "result", id: "fork", status: 200, body: { success: true, data: {} } });
  assert.deepEqual(out.calls[1].body, { type: "prompt", message: "second" });
  for (let i = 0; i < 600; i++) ev(10 + i, { type: "tool_execution_start" });
  assert.match(JSON.stringify(ev(999, { type: "agent_start" }).view), /"500 events"/);
});

test("chat keeps only the latest 500 items", async () => {
  const c = await load("chat.js");
  c.update({ type: "open", session: "ab12" });
  for (let i = 0; i < 700; i++) c.update({ type: "event", stream: "events", data: { type: "tool_execution_start", toolName: "t" + i } });
  const v = JSON.stringify(c.update({ type: "event", stream: "events", data: { type: "agent_start" } }).view);
  assert.equal(v.split("tool: ").length - 1, 500);
  assert.match(v, /t699/);
});
