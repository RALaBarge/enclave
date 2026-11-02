// Pi extension: the desk's tools, and nothing else.
//
// pi-bridge starts Pi with every built-in tool and extension switched off (PI_LOCKDOWN in pibridge.py) and loads
// this one file with -e. At load it asks the desk for its tools (GET /tools, then GET /tools/{name} for each
// contract) and registers one Pi tool per desk tool, with the desk's input schema as the parameters. A call is
// POST /tools/{name} with {"input": ...}: the data the agent works on travels in the request, and the result comes
// back in the response. There is no other primitive here: no exec, no file access, no fetch to anywhere but the desk.
//
// No desk at load means no tools: the session can still talk, it cannot act. Wire format and limits follow
// harness/toolsadapter/desk_tools.py, which is the reference implementation.

const DESK = (process.env.PI_DESK_URL || "http://127.0.0.1:8080").replace(/\/+$/, "");
const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;
const LIST_TIMEOUT_MS = 5000;
const CALL_TIMEOUT_MS = 70000; // the desk caps ?wait= at 55 s and answers 202 when it elapses
const WAIT = "45s";
// A job still queued or running after one wait is polled again (GET /jobs/{id}/wait) until it ends or this passes. A
// sandbox call can sit behind the previous job's reset (a full-mode reboot), so one 45 s wait is not the job's budget;
// the contract's timeout_ms is, and the desk ends the job by then.
const JOB_MAX_MS = 15 * 60 * 1000;

async function desk(path, { body, timeoutMs, signal } = {}) {
	const signals = [AbortSignal.timeout(timeoutMs)];
	if (signal) signals.push(signal);
	const res = await fetch(DESK + path, {
		method: body === undefined ? "GET" : "POST",
		headers: body === undefined ? { Accept: "application/json" } : { "Content-Type": "application/json" },
		body: body === undefined ? undefined : JSON.stringify(body),
		signal: AbortSignal.any(signals),
	});
	const text = await res.text();
	let doc;
	try {
		doc = JSON.parse(text);
	} catch {
		throw new Error(`desk ${path}: HTTP ${res.status}, not JSON: ${text.slice(0, 200)}`);
	}
	if (!res.ok) {
		const v = doc && doc.violations ? "; violations: " + JSON.stringify(doc.violations) : "";
		throw new Error(`desk ${path}: HTTP ${res.status}: ${(doc && doc.error) || text.slice(0, 200)}${v}`);
	}
	return doc;
}

function inputSchema(name, raw) {
	if (raw === undefined || raw === null || (typeof raw === "object" && Object.keys(raw).length === 0))
		return { type: "object", properties: {} };
	if (typeof raw !== "object" || Array.isArray(raw)) throw new Error(`${name}: input schema is not an object`);
	if (raw.type !== undefined && raw.type !== "object") throw new Error(`${name}: input type ${raw.type}, not object`);
	return raw.type === undefined && !raw.oneOf && !raw.anyOf ? { ...raw, type: "object" } : raw;
}

export async function deskTools() {
	const listed = await desk("/tools", { timeoutMs: LIST_TIMEOUT_MS });
	if (!Array.isArray(listed)) throw new Error("desk /tools: not a JSON array");
	const tools = [];
	for (const entry of listed) {
		const name = entry && entry.name;
		if (typeof name !== "string" || !NAME_RE.test(name)) throw new Error(`desk listed an unusable name: ${name}`);
		if (tools.some((t) => t.name === name)) throw new Error(`desk listed ${name} twice`);
		const c = await desk("/tools/" + encodeURIComponent(name), { timeoutMs: LIST_TIMEOUT_MS });
		if (!c || typeof c !== "object" || (c.name !== undefined && c.name !== name))
			throw new Error(`desk /tools/${name}: not the contract for ${name}`);
		const summary = c.summary ?? entry.summary ?? "";
		if (typeof summary !== "string") throw new Error(`${name}: summary is not a string`);
		tools.push({ name, summary, parameters: inputSchema(name, c.input) });
	}
	return tools.sort((a, b) => (a.name < b.name ? -1 : 1));
}

export async function deskCall(name, input, signal) {
	const path = "/tools/" + encodeURIComponent(name) + "?wait=" + WAIT;
	const deadline = Date.now() + JOB_MAX_MS;
	let job = await desk(path, { body: { input: input ?? {} }, timeoutMs: CALL_TIMEOUT_MS, signal });
	while (job && (job.status === "queued" || job.status === "running") && Date.now() < deadline) {
		if (typeof job.id !== "string" || !NAME_RE.test(job.id)) throw new Error(`desk returned a bad job id`);
		job = await desk("/jobs/" + job.id + "/wait?timeout=" + WAIT, { timeoutMs: CALL_TIMEOUT_MS, signal });
	}
	const status = job && job.status;
	if (status === "done") return job.result ?? null; // a null result is omitted from the job, not a failure
	if (status === "failed" || status === "canceled")
		throw new Error(`desk job ${job.id} ${status}: ${job.error || "no error message"}`);
	if (status === "queued" || status === "running")
		throw new Error(`desk job ${job.id} still ${status} after ${JOB_MAX_MS / 60000} min`);
	throw new Error(`desk job ${job && job.id} has unknown status ${status}`);
}

// Inline & Spill: bound what reaches the model, keep a way back.
//
// The desk's tools already bound themselves — read answers at most 250,000 chars and reports total_lines, grep caps
// at 500 matches, bash cuts stdout and stderr at 30,000 each. What none of them decides is how much of that belongs
// in the next request. A single read of a large file injects a quarter of a megabyte into the context and dilutes
// every instruction around it; this is the policy that stops it.
//
// Only string leaves are cut, so exit_code, timed_out, total_lines, truncated and error survive: a blind cut of the
// serialised form drops exactly the fields that say whether the result is complete. The reference left behind is the
// call, not a handle — every tool that can return a large result can return a narrower one, so nothing has to be
// stored in the core. That is the deliberate difference from the design this borrows from: there a refetch is a
// local file read, here it is a sandbox job behind a serial lane and, in box mode, a reboot. A refetch must be rare
// and chosen, so this policy never issues one on the model's behalf.
//
// harness/toolsadapter/inline.py is the reference implementation and the test oracle for this copy; the two are kept
// behaviourally identical and must be changed together.

const FIELD_FLOOR = 512; // a shorter field is not worth a marker
const HEAD_RATIO = 0.6; // head carries the imports and the first errors; tail the summary
const MAX_ITEMS = 200; // so a 500-match grep cannot force 500 markers
const INLINE_MAX_CHARS = (() => {
	const raw = Number(process.env.PI_INLINE_MAX_CHARS);
	return Number.isFinite(raw) && raw >= FIELD_FLOOR ? Math.floor(raw) : 16000;
})();

const HINTS = {
	content: "re-issue with offset/limit, or a command that prints less",
	stdout: "re-issue with offset/limit, or a command that prints less",
	stderr: "re-issue with offset/limit, or a command that prints less",
	text: "re-issue with offset/limit, or a command that prints less",
	output: "re-issue with offset/limit, or a command that prints less",
	matches: "tighten the pattern or set a narrower path",
};
const GENERIC_HINT = "re-issue the call with a narrower range";
const listMarker = (n) => `…[${n} more items elided; narrow the call to see them]`;
const marker = (elided, total, hint) => `\n…[${elided} of ${total} chars elided; ${hint}]\n`;
const sizeOf = (v) => JSON.stringify(v).length;

function hintFor(path) {
	const leaf = (path.split(".").pop() || "").replace(/(\[\d+\])+$/, "");
	return HINTS[leaf] || GENERIC_HINT;
}

function cut(text, cap, total, hint) {
	if (text.length <= cap) return text;
	const body = Math.max(0, cap - marker(text.length - cap, total, hint).length);
	if (body <= 0) return text.slice(0, cap);
	const headLen = Math.floor(body * HEAD_RATIO);
	const tailLen = body - headLen;
	let head = text.slice(0, headLen);
	const breakAt = head.lastIndexOf("\n");
	if (breakAt > headLen / 2) head = head.slice(0, breakAt);
	let tail = tailLen ? text.slice(text.length - tailLen) : "";
	if (tail) {
		const resume = tail.indexOf("\n");
		if (resume >= 0 && resume < tailLen / 2) tail = tail.slice(resume + 1);
	}
	return head + marker(text.length - head.length - tail.length, text.length, hint) + tail;
}

function rewrite(value, fn, path = "") {
	if (typeof value === "string") return fn(path, value);
	if (Array.isArray(value)) return value.map((v, i) => rewrite(v, fn, `${path}[${i}]`));
	if (value && typeof value === "object") {
		const out = {};
		for (const k of Object.keys(value)) out[k] = rewrite(value[k], fn, path ? `${path}.${k}` : k);
		return out;
	}
	return value;
}

function capLists(value, limit, info) {
	if (Array.isArray(value)) {
		let kept = value;
		if (value.length > limit) {
			info.items_elided += value.length - limit;
			kept = value.slice(0, limit).concat([listMarker(value.length - limit)]);
		}
		return kept.map((v) => capLists(v, limit, info));
	}
	if (value && typeof value === "object") {
		const out = {};
		for (const k of Object.keys(value)) out[k] = capLists(value[k], limit, info);
		return out;
	}
	return value;
}

// Shorten the fields first, and only if that cannot fit, keep fewer list items: sixteen whole rows beat two hundred
// rows of forty characters. The item limit is the outer loop and the per-field cap the inner one, and each pass starts
// from the original so the answer depends only on the input (inline.py's tests diff the two implementations).
export function inlineApply(value, maxChars = INLINE_MAX_CHARS) {
	const original = sizeOf(value);
	const info = {
		original_chars: original,
		final_chars: original,
		elided_chars: 0,
		fields_elided: [],
		items_elided: 0,
		hard_cut: false,
	};
	if (original <= maxChars) return { value, info };
	let best = null;
	for (const itemLimit of [MAX_ITEMS, 50, 16, 5]) {
		const trial = { ...info, fields_elided: [], items_elided: 0 };
		const base = capLists(value, itemLimit, trial);
		let cap = Math.max(FIELD_FLOOR, Math.floor(maxChars / 2));
		let candidate;
		let size;
		let fields = [];
		for (;;) {
			fields = [];
			candidate = rewrite(base, (p, t) => {
				if (t.length <= cap) return t;
				fields.push(p);
				return cut(t, cap, original, hintFor(p));
			});
			size = sizeOf(candidate);
			if (size <= maxChars || cap <= FIELD_FLOOR) break;
			cap = Math.max(FIELD_FLOOR, Math.floor(cap / 2));
		}
		trial.fields_elided = fields;
		trial.final_chars = size;
		trial.elided_chars = Math.max(0, original - size);
		if (!best || size < best.size) best = { size, value: candidate, info: trial };
		if (size <= maxChars) break;
	}
	return { value: best.value, info: best.info };
}

// The text to put in front of the model, never longer than maxChars. Anything with string leaves to cut gets there by
// shrinking fields; a result with nothing worth cutting (a million tiny keys) gets a marked hard cut instead, so an
// over-budget result is never silently over budget.
export function inlineText(result, maxChars = INLINE_MAX_CHARS) {
	const original = sizeOf(result);
	const { value, info } = inlineApply(result, maxChars);
	let text = typeof value === "string" ? value : JSON.stringify(value);
	if (text.length > maxChars) {
		text = cut(text, maxChars, text.length, GENERIC_HINT);
		info.hard_cut = true;
		info.final_chars = text.length;
		info.elided_chars = Math.max(0, original - text.length);
	}
	return { text, info };
}

export default async function (pi) {
	let tools;
	try {
		tools = await deskTools();
	} catch (e) {
		console.error(`desk-extension: no tools: ${e.message}`);
		return;
	}
	for (const t of tools) {
		pi.registerTool({
			name: t.name,
			label: t.name,
			description: t.summary,
			parameters: t.parameters,
			async execute(_id, params, signal) {
				const result = await deskCall(t.name, params, signal);
				const { text, info } = inlineText(result);
				return { content: [{ type: "text", text }], details: { desk: t.name, inline: info } };
			},
		});
	}
}
