// Tests for desk-extension.js against a fake desk. No Pi needed.
// Run: node --test harness/bridge/tests/test_desk_extension.mjs
import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const calls = [];
let job = { id: "j1", status: "done", result: { greeting: "hi" } };
let polls = [];
const desk = http.createServer((req, res) => {
	let body = "";
	req.on("data", (c) => (body += c));
	req.on("end", () => {
		calls.push({ method: req.method, url: req.url, body });
		const send = (code, doc) => res.writeHead(code, { "Content-Type": "application/json" }).end(JSON.stringify(doc));
		if (req.url === "/tools") return send(200, [{ name: "greet-sbx", summary: "list summary" }]);
		if (req.url === "/tools/greet-sbx")
			return send(200, { name: "greet-sbx", summary: "Say hello", input: { properties: { name: { type: "string" } } } });
		if (req.url.startsWith("/tools/greet-sbx?wait=")) return send(job.status === "done" ? 200 : 202, job);
		const m = req.url.match(/^\/jobs\/([^/]+)\/wait\?timeout=45s$/);
		if (m) return send(200, polls.shift() ?? job);
		send(404, { error: "no such route" });
	});
});

let ext;
before(async () => {
	await new Promise((r) => desk.listen(0, "127.0.0.1", r));
	process.env.PI_DESK_URL = `http://127.0.0.1:${desk.address().port}/`;
	// Node reads a .js file as CommonJS here; Pi's loader takes it as a module. Import a .mjs copy.
	const copy = path.join(mkdtempSync(path.join(tmpdir(), "desk-ext-")), "desk-extension.mjs");
	copyFileSync(path.join(here, "..", "desk-extension.js"), copy);
	ext = await import(copy);
});
after(() => desk.close());

test("registers one tool per desk tool, with the contract's schema", async () => {
	const registered = [];
	await ext.default({ registerTool: (t) => registered.push(t) });
	assert.deepEqual(registered.map((t) => [t.name, t.description]), [["greet-sbx", "Say hello"]]);
	assert.deepEqual(registered[0].parameters, { properties: { name: { type: "string" } }, type: "object" });

	calls.length = 0;
	const out = await registered[0].execute("call-1", { name: "ryan" }, undefined);
	assert.deepEqual(out.content, [{ type: "text", text: '{"greeting":"hi"}' }]);
	assert.equal(calls[0].method, "POST");
	assert.equal(calls[0].url, "/tools/greet-sbx?wait=45s");
	assert.deepEqual(JSON.parse(calls[0].body), { input: { name: "ryan" } });
});

test("a job still queued after the first wait is polled until it ends", async () => {
	job = { id: "j5", status: "queued" };
	polls = [{ id: "j5", status: "running" }, { id: "j5", status: "done", result: { greeting: "late" } }];
	calls.length = 0;
	assert.deepEqual(await ext.deskCall("greet-sbx", {}), { greeting: "late" });
	assert.deepEqual(calls.map((c) => c.url), ["/tools/greet-sbx?wait=45s", "/jobs/j5/wait?timeout=45s", "/jobs/j5/wait?timeout=45s"]);
	job = { id: "../x", status: "queued" };
	await assert.rejects(ext.deskCall("greet-sbx", {}), /bad job id/);
	job = { id: "j1", status: "done", result: { greeting: "hi" } };
});

test("a failed job is an error, never a result", async () => {
	for (const j of [{ id: "j2", status: "failed", error: "boom" }, { id: "j3", status: "canceled" }]) {
		job = j;
		await assert.rejects(ext.deskCall("greet-sbx", {}), new RegExp(j.status));
	}
	job = { id: "j4", status: "done" };
	assert.equal(await ext.deskCall("greet-sbx", {}), null);
});

test("no desk means no tools", async () => {
	const saved = process.env.PI_DESK_URL;
	// DESK is read at import, so load a fresh copy pointed at a closed port.
	process.env.PI_DESK_URL = "http://127.0.0.1:9";
	const copy = path.join(mkdtempSync(path.join(tmpdir(), "desk-ext-")), "desk-extension.mjs");
	copyFileSync(path.join(here, "..", "desk-extension.js"), copy);
	const fresh = await import(copy);
	process.env.PI_DESK_URL = saved;
	const registered = [];
	const err = console.error;
	console.error = () => {};
	try {
		await fresh.default({ registerTool: (t) => registered.push(t) });
	} finally {
		console.error = err;
	}
	assert.deepEqual(registered, []);
});

test("the extension has no primitive but fetch to the desk", async () => {
	const { readFileSync } = await import("node:fs");
	const src = readFileSync(path.join(here, "..", "desk-extension.js"), "utf8");
	assert.doesNotMatch(src, /\bimport\b|\brequire\s*\(|child_process|node:fs|\beval\s*\(|new Function/);
});
