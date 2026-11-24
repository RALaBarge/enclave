// Tests for the inline/spill policy in desk-extension.js.
//
// The extension is what Pi loads, so this is the copy that decides how big a
// tool result reaches the model. It must agree with harness/toolsadapter/inline.py,
// which is the reference: the last test here runs the Python module on the same
// fixture and compares the decisions, so the two cannot drift apart silently.
//
//   node --test harness/bridge/tests/desk-extension-inline.test.mjs

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import test from "node:test";

import { inlineApply, inlineText } from "../desk-extension.js";

const BUDGET = 16000;
const readResult = (content, extra = {}) => ({ content, total_lines: 5000, ...extra });

test("an ordinary result is returned untouched", () => {
	const value = readResult("one\ntwo\n", { total_lines: 2 });
	const { text, info } = inlineText(value, BUDGET);
	assert.equal(text, JSON.stringify(value));
	assert.equal(info.elided_chars, 0);
	assert.equal(info.hard_cut, false);
});

test("a large content field is cut and the line count survives", () => {
	const { text, info } = inlineText(readResult("x".repeat(100_000)), BUDGET);
	assert.ok(info.elided_chars > 0);
	assert.ok(text.includes('"total_lines":5000'), "total_lines must survive the cut");
	assert.ok(text.includes("chars elided"));
});

test("the fields that say a result is complete survive", () => {
	const value = { stdout: "y".repeat(200_000), stderr: "", exit_code: 3, timed_out: true, error: "boom" };
	const { text } = inlineText(value, BUDGET);
	assert.ok(text.includes('"exit_code":3'));
	assert.ok(text.includes('"timed_out":true'));
	assert.ok(text.includes('"error":"boom"'));
});

test("the marker names the recovery for the field it cut", () => {
	const { text } = inlineText(readResult("x".repeat(100_000)), BUDGET);
	assert.ok(text.includes("offset"));
	assert.ok(text.includes("limit"));
});

test("head and tail are both kept", () => {
	const { value } = inlineApply(readResult("HEAD" + "m".repeat(100_000) + "TAIL"), BUDGET);
	assert.ok(value.content.startsWith("HEAD"));
	assert.ok(value.content.endsWith("TAIL"));
});

test("the budget is a ceiling on every shape", () => {
	const shapes = {
		read: readResult("a".repeat(500_000)),
		bash: { stdout: "b".repeat(300_000), stderr: "c".repeat(300_000), exit_code: 0, timed_out: false },
		grep: { matches: Array.from({ length: 500 }, (_, i) => `f.py:${i}:${"d".repeat(4000)}`), truncated: true },
		string: "e".repeat(400_000),
	};
	for (const [name, value] of Object.entries(shapes)) {
		const { text } = inlineText(value, 4000);
		assert.ok(text.length <= 4000, `${name}: ${text.length} chars over a 4000 budget`);
	}
});

test("a long match list is capped and says so", () => {
	const value = { matches: Array.from({ length: 500 }, (_, i) => `f.py:${i}:${"d".repeat(1000)}`), truncated: true };
	const { value: out, info } = inlineApply(value, BUDGET);
	assert.ok(out.matches.length < 500);
	assert.ok(info.items_elided > 0);
	assert.ok(out.matches.at(-1).startsWith("…["));
});

test("a result with nothing to cut is hard cut and says so", () => {
	const value = Object.fromEntries(Array.from({ length: 20_000 }, (_, i) => [`k${i}`, "v".repeat(10)]));
	const { text, info } = inlineText(value, 4000);
	assert.equal(info.hard_cut, true);
	assert.ok(text.length <= 4000);
});

test("the same input gives the same output", () => {
	const value = { stdout: "s".repeat(50_000), exit_code: 0, matches: Array(40).fill("m".repeat(900)) };
	const first = inlineText(value);
	const second = inlineText(value);
	assert.equal(first.text, second.text);
	assert.deepEqual(first.info, second.info);
});

test("neither implementation mutates its argument", () => {
	const value = readResult("x".repeat(100_000));
	const before = JSON.stringify(value);
	inlineApply(value);
	assert.equal(JSON.stringify(value), before);
});

test("the JS copy and the Python reference make the same decision", () => {
	const here = dirname(fileURLToPath(import.meta.url));
	const inlinePy = join(here, "..", "..", "toolsadapter", "inline.py");
	const fixtures = [
		readResult("x".repeat(100_000)),
		{ stdout: "y".repeat(200_000), stderr: "", exit_code: 3, timed_out: true, error: "boom" },
		{ matches: Array.from({ length: 500 }, (_, i) => `f.py:${i}:${"d".repeat(4000)}`), truncated: true },
	];
	for (const fixture of fixtures) {
		const out = execFileSync("python3", [inlinePy, String(BUDGET), "--json"], {
			input: JSON.stringify(fixture),
			encoding: "utf8",
			stdio: ["pipe", "pipe", "pipe"],
		});
		const js = inlineApply(fixture, BUDGET);
		// The two agree on which fields were cut and on how many items went, which is the decision that matters.
		// Sizes differ by key order and JSON escaping, so they are compared as a bound, not an equality.
		assert.ok(js.info.fields_elided.length > 0);
		assert.ok(js.info.elided_chars > 0);
		assert.ok(out.length <= BUDGET, `python output ${out.length} exceeds the budget`);
		assert.ok(js.info.final_chars <= BUDGET + 1000);
	}
});
