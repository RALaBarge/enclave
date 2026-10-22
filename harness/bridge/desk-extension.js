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
				const text = typeof result === "string" ? result : JSON.stringify(result);
				return { content: [{ type: "text", text }], details: { desk: t.name } };
			},
		});
	}
}
