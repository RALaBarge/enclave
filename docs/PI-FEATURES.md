# Pi 1.2.0 features, classified for an in-tree Enclave harness

2026-10-20. Source: `earendil-works/pi` at the pinned release commit `06a46693`, about 170k lines of TypeScript.
This is option 3 from `HARNESS-HARDENING-PLAN.md`: replace Pi in the core with a small in-tree runner (a static Go binary, one process per
turn, started by the bridge), writing in the features we want. Revised 2026-10-20 after an outside review.

## Why replace Pi

The UI calls exactly six commands: `prompt`, `abort`, `get_messages`, `get_tree`, `fork`, `get_fork_messages`. Enclave turns
off all of Pi's tools, extensions, MCP, skills and context files. What's left is a model loop and a session history, and
today all 170k lines of Pi run inside the trusted core, the part that holds the keys.

There are three ways to handle it:

| Option | Verdict |
|---|---|
| 1. Lock Pi down from the bridge (`--no-*` flags, RPC allowlist, version pin) | **Stopgap.** About 50 lines and quick, and it closes today's real holes, including RPC `bash`. But it's a denylist against a big codebase: we trust that `--no-builtin-tools` and `--no-extensions` do what they say, RPC `bash` already showed that "tools off" doesn't mean everything is off, and every Pi upgrade needs auditing again. |
| 2. Fork Pi and patch the tools out in `build-pi.sh` | **Rejected.** We'd still carry all 170k lines, plus a patch against a fast-moving upstream. Only a fallback if Pi drops the flags. |
| 3. Replace Pi with an in-tree loop of our own (one static binary, one process per turn) | **Target.** The trusted part shrinks by more than 99%. It's a supply-chain win as well as a size win: Node, Bun, Pi's npm dependency tree and `build-pi.sh` all leave the unit that holds the keys, so the agent loop in the core is one static binary and its standard library. There are no tools to disable because none exist; the desk's are the only tools. Admission item 1 becomes something you can check by reading the code: no exec, no file access and no network in the loop except the provider call. The session format stays plain JSONL you can `grep` and `jq`. Features Pi gives us are written in rather than given up (this document). |

Order: do option 1 and the RPC allowlist now (`HARNESS-HARDENING-PLAN.md` steps 1–2), then build option 3 and delete Pi from the image.

Answers to the two open questions (defaults; Ryan to confirm the first):
- If nothing outside Enclave runs Pi interactively, Pi leaves the image entirely. If something does, it stays installed on
  that machine, not in the core.
- Keep `docs/HARNESS-ADMISSION.md` and make the in-tree loop its reference implementation. The checklist then stops being a
  gate for one harness and becomes a spec for any harness.

## Process model (decision)

**One process per turn, and it has no control channel.** Isolation still comes from the process: a bad run can't take down
the bridge, the keys or another session. But the process lives only as long as the turn, because nothing needs to talk to it
while it runs. The UI sends `prompt`, `abort`, `get_messages`, `get_tree`, `fork` and `get_fork_messages`, and never `steer`.
The only other input is peer delivery (`deliver()` in `server.py`), which sends `streamingBehavior: "followUp"` — a queue
drained between runs, not a mid-run injection. So the runner is a command-line tool, not a server, and there is no protocol
to define.

The session directory is the whole interface:

    sessions/<id>/session.jsonl   the truth; written only by the runner, one entry per line, fsynced before the event
    sessions/<id>/events.jsonl    a cache, never fsynced; the bridge tails it by byte offset for SSE
    sessions/<id>/inbox/          prompts, maildir style: write under tmp/, rename into new/
    sessions/<id>/tools.json      the last tool list from the desk; written only by the runner
    sessions/<id>/last-exit       the last run's exit code; written only by the bridge, which is the parent

- **Prompts:** UI prompts and peer messages are both inbox files. If the session is not locked, the bridge runs
  `loop run <dir>`. The runner takes an exclusive `flock` on `session.jsonl`, turns each inbox file into a user entry, and
  deletes the file only after that entry is fsynced. It runs the turn, checks the inbox again, and exits when it is empty,
  which is what follow-ups already do. The bridge pumps once more after the process exits, so a message that lands just
  before an exit is not stranded. At startup it scans every `inbox/new/` and starts a runner wherever files are waiting,
  which is the whole recovery story: the queue survives a bridge restart, which today's in-memory one does not, and no
  process outlives the bridge.
- **Abort:** one signal. With one process per turn, `abort` and `stop` are the same thing: commit the aborted entry, record
  `"Operation aborted"` for a call in flight, exit. `SIGINT`, then `SIGKILL` after a timeout as a backstop.
- **Liveness:** the `flock`. The kernel drops it when the process dies, so there is no pid file to go stale and no pid reuse
  to defend against.
- **Reads are subcommands of the same binary**, the way `git cat-file` works: `loop tree <dir>` and `loop messages <dir>`
  answer `get_tree`/`get_messages` while a run is going, so the branch logic, compaction included, exists once. `loop fork`
  and `loop clone` are the same kind of thing. The bridge execs the binary it already runs, so this adds no new exec to the
  core.

**Go, not Python, for the runner.** One static binary, standard library only.
- Abort needs a real cancel. `signal.NotifyContext` drops an in-flight provider request at once. In Python, PEP 475 retries
  the interrupted read, so a stalled stream holds the process until its socket timeout, and the workaround is a self-pipe or
  closing the socket from a handler.
- Admission item 1 becomes mechanical rather than a tripwire. `go list -deps` gives the import closure, so a build-time
  check fails on `os/exec`, `plugin`, `unsafe`, or on `net`, `net/http` and `crypto/tls` outside the two client packages.
  A Python AST walk can be evaded with `importlib`; Go's package graph cannot.
- Cost: a second language, a pinned static binary with its sha256 in `manifest.env` as Ollama, Pi and DeskBox already have,
  and `HARNESS-HARDENING-PLAN.md`'s "stdlib only (Python)" rule becomes "standard library only, and no third-party modules in either
  language".

Rules that come with owning the child:
- **Narrow environment.** Today `_spawn_pipe` passes no `env=`, so Pi inherits everything the bridge holds, including both
  provider keys from `secrets.env`. The runner gets an explicit small environment: the one provider key for the session's
  model, the desk URL, `PATH`, `HOME` and `LANG`. Nothing else. A static binary needs no interpreter and no `PYTHONPATH`
  hygiene.
- **The runner owns the session file.** Fsync-before-emit only holds in the process that emits, so only the runner writes it;
  the bridge reads it through the subcommands and never writes it.
- **The header records the engine.** The session header gets `"engine": "pi"` or `"engine": "loop"` plus its version, so a
  mixed set of sessions during the transition can be told apart. Policy: either engine may continue the other's session once
  cross-continuation tests pass (read and continue a Pi file with the loop, and the reverse). Until then the bridge refuses
  to continue a session with the other engine.

Rejected: **every session in the bridge process** (one address space for all contexts, keys and streams — a regression in the
process that holds the keys) and **one long-lived runner per session** (it would exist to receive mid-run input, and there is
no mid-run input).

### Rules that follow

- **The unit goes back to `KillMode=control-group`.** Orphans exist only because `harness/systemd/pi-bridge.service` sets
  `KillMode=process`, whose comment says why: long-lived Pi processes survive a bridge restart and it reattaches over their
  FIFOs. One process per turn drops reattach, so that reason is gone. A bridge restart then takes an in-flight run with it —
  `SIGTERM`, then `SIGKILL` if it does not stop — and the runner handles `SIGTERM` exactly like `SIGINT`, one handler, with
  the aborted-run rule repairing anything `SIGKILL` catches. The cost is at most one in-flight turn; unclaimed prompts stay
  in `inbox/` and the startup scan picks them up. Nothing then writes a session without the bridge as its parent, so
  `last-exit` is always collected. It also closes a gap: a restarted bridge could not abort an orphan anyway, since there is
  no pid file left and finding the lock holder would mean parsing `/proc/locks`. The "use `systemctl kill`, not
  `systemctl stop`" note goes away with it.
- **A setting with no entry type is a constant.** `set_auto_compaction`, `set_auto_retry` and the queue modes have no home in
  a per-turn design, the UI never sends them and there is one user, so they are constants in the code and there is no settings
  file. `set_model`, `set_thinking_level` and the session name already have entry types and stay in the session file.
- **`last-exit` is written by the bridge.** The bridge is the parent, so `wait()` hands it an exit code even when the runner
  was `SIGKILL`ed and could write nothing. Keeping that file bridge-owned leaves `events.jsonl` with one writer. The UI then
  shows "alive" as "the lock is held" and flags only exit codes that are not 0; "exited" is the normal state between turns,
  not an error.
- **A killed run is repaired by the same rule as an aborted one.** The request builder from section 1 (skip `aborted`
  entries, synthesize `"No result provided"` for an orphaned call) covers both, in one place. That is what makes the `SIGKILL`
  backstop safe rather than hopeful.
- **Readers ignore a last line with no trailing newline.** Regular files give no one-`write()` atomicity the way pipes do, and
  the `flock` already makes the writer single.

## Classes

| Class | Meaning |
|---|---|
| **WRITE** | Needed for Enclave's loop. Write it in-tree now. |
| **LATER** | Useful, not needed for the first cut. Write it when a real need shows up. |
| **NIXED** | Already decided off for Enclave (the plan disables it in stock Pi too). |
| **HAVE** | Enclave already does this some other way. |
| **DROP** | Doesn't apply: terminal UI, distribution, vendor accounts, telemetry. |

Line counts are rough estimates in the runner's style, tests not included.

## 1. Agent loop

| Feature | Class | Notes |
|---|---|---|
| Turn loop: prompt -> model -> tool calls -> results -> repeat until no tool calls | **WRITE** | The core, about 150 lines. |
| Steering and follow-up messages (`steer`, `follow_up`) | **WRITE** | Neither is a mid-run injection: a prompt that arrives during a run is one more `inbox/` file, and the runner drains the inbox between turns, which is what a follow-up already is. The command names stay on the HTTP API and write an inbox file; there is no queue, so there are no queue modes. `clear_queue` deletes unclaimed inbox files, and `queue_update` is the inbox listing. |
| `abort` (stop the run) | **WRITE** | `SIGINT` to the runner, `SIGKILL` after a timeout. The runner commits the aborted entry and exits. A desk job already running in the sandbox **finishes anyway**, so there are three written rules: (1) a result that arrives after an abort is discarded and never appended to the session; (2) an aborted run always leaves a state the UI can render, meaning an `aborted` assistant entry and no dangling tool call; (3) a stream that dies mid-tool-call never executes the partially assembled call. Real cancellation in the broker is protocol work, listed under LATER. **Closing rule:** an aborted turn leaves an assistant entry with tool calls and no results, which most providers reject on the next request. Do what Pi does (`transform-messages.ts`): the session file records what actually happened (the `aborted` entry, plus an `"Operation aborted"` error result for a call that was executing), and the request builder skips `error`/`aborted` assistant entries and inserts a `"No result provided"` tool result for any orphaned call. The file stays honest, the next request is well formed, and the behavior matches Pi's sessions in the corpus. |
| Broker-level job cancellation | **LATER** | Needs a job-id protocol between stub and broker (same root cause as review finding A). |
| Auto-retry with exponential backoff (`retry.*`, `set_auto_retry`, `abort_retry`, `auto_retry_*` events) | **WRITE** | About 50 lines. Transient HTTP errors only. |
| Provider-level vs. agent-level retry split | **DROP** | One retry layer is enough. |
| HTTP timeouts (`httpIdleTimeoutMs`, provider `timeoutMs`) | **WRITE** | Socket timeout and per-request deadline. |
| Stop reasons and length recovery (`length`, overflow) | **WRITE** | Feeds compaction (section 4). |
| Nested tool calls (a tool calling tools, `ctx.executeTool`) | **NIXED** | Only exists for codemode. |
| Parallel tool execution | **HAVE** | The broker runs one sandbox lane in FIFO order; calls go through it serially. |

## 2. Models and providers

| Feature | Class | Notes |
|---|---|---|
| OpenAI-compatible Chat Completions wire format, streaming SSE | **WRITE** | Covers OpenRouter, Requesty, DeepSeek, oMLX, llama.cpp, vLLM. About 200 lines including the SSE parser. |
| Streaming tool-call assembly (partial JSON across deltas) | **WRITE**, one of the two hard parts | Accumulate by `index`, handle several calls in one stream, JSON split across deltas, `finish_reason` arriving before or after the last delta, and providers that send the whole call in one chunk. Tested against recorded real streams (see Testing), not just hand-written cases. |
| Thinking/reasoning content (stream `reasoning` deltas, store thinking blocks) | **WRITE** | About 40 lines. |
| Thinking levels (`off` ... `max`) mapped to provider params, `set_thinking_level`, `cycle_thinking_level`, `get_available_thinking_levels` | **WRITE** | A per-model table in the pinned model list. |
| Per-model thinking defaults and budgets (`modelThinkingLevels`, `thinkingBudgets`) | **WRITE** | Same table. |
| Pinned model list: `set_model`, `cycle_model`, `get_available_models` | **WRITE** | Plan step 8. Only models on the list can be chosen. |
| Model change mid-session (handoff between models) | **WRITE** | Drop thinking blocks the new model can't read; about 20 lines. |
| Anthropic Messages wire format | **LATER** | Only if we use Anthropic directly instead of through OpenRouter. |
| OpenAI Responses, Google, Vertex, Bedrock, Mistral, Azure, Cloudflare and the other ~30 built-in providers | **DROP** | Routers and OpenAI-compatible endpoints cover what we use. |
| Remote model catalog from pi.dev, `models.generated.ts`, catalog refresh | **DROP** | We pin our own list. It's also an outbound call from the core. |
| Virtual models (catalog entries that route to physical models) | **DROP** | |
| Radius gateway provider | **DROP** | |
| Classifier models (OpenAI Decisions, TypeSafe System One, llama.cpp classify) | **DROP** | |
| Image generation (OpenRouter images) | **DROP** | |
| Image input (`ImageContent`, `images.autoResize`, `images.blockImages`) | **LATER** | Uploads of screenshots. Base64 in the message, about 30 lines. |
| Prompt caching hints (OpenAI prompt cache key, Anthropic `cache_control`) | **LATER** | Cost, not function. |
| Cache warming (`cacheWarming`, background requests to keep caches hot) | **DROP** | Extra outbound traffic for a small saving. |
| Constrained sampling for tools | **DROP** | |
| Transport choice (`websocket`, `websocket-cached`) | **DROP** | SSE only. |
| `httpProxy` | **DROP** | |
| Tool-list check on every request | **WRITE** | The loop builds each request itself, so before sending it asserts that the `tools` array holds only desk tool names, and refuses the request otherwise. It needs names only, so nothing is written to disk. |
| Debugging provider payloads | **LATER**, off by default | A full request dump is a data-at-rest hazard: raw conversation and tool arguments on disk, outside the `output_redactor` (which `server.py` applies only at the Loom boundary). If it's ever needed, it goes behind an explicit debug flag, runs through the redactor, stays off in the image, and writes through the session module's writer (so it fsyncs and lands in the one audited writer) instead of its own `open()`, which the tripwire would rightly refuse. |

## 3. Usage and cost

| Feature | Class | Notes |
|---|---|---|
| Token usage per response (`Usage`, `UsageEntry`) | **WRITE** | From the provider's `usage` field. |
| Session stats (`get_session_stats`) | **WRITE** | Sum of usage entries. |
| Cost tracking | **WRITE** | A price per model in the pinned list; about 20 lines. A pinned model with no price fails the list check at startup, so cost is never silently 0. If a provider leaves out `usage`, the entry records `null`, not 0. |
| Cache-miss notices, diagnostics | **DROP** | |

## 4. Context and compaction

| Feature | Class | Notes |
|---|---|---|
| System prompt (base plus `--append-system-prompt`) | **WRITE** | A fixed file in the image plus an optional appendix. |
| Token estimate (characters / 4) for thresholds | **WRITE** | |
| Auto-compaction at threshold (`compaction.enabled`, `reserveTokens`, `keepRecentTokens`) | **WRITE** | About 120 lines: pick a cut point, ask the model for a summary, insert a `CompactionEntry`. |
| Manual `compact` with instructions, `set_auto_compaction` | **WRITE** | Same code path. |
| Cut-point rules (never split a tool call from its result; split long user spans) | **WRITE** | Part of the above. |
| Summary format (Goal / Constraints / Progress / Decisions / Next steps / Critical context) | **WRITE** | A prompt file. |
| Overflow and length recovery ordering | **WRITE** | Compact, then retry once. |
| Per-model compaction overrides | **WRITE** | In the pinned model table. |
| Branch summaries (summarize the branch you leave when moving in the tree) | **LATER** | The UI can fork without it. |
| Cumulative file tracking in summaries | **DROP** | Built for Pi's file tools; desk tools don't edit local files. |
| Context files (`AGENTS.md` / `CLAUDE.md` discovery) | **NIXED** | Plan step 1 (`--no-context-files`). |
| Skills (on-demand instructions) | **NIXED** | |
| Prompt templates | **NIXED** | |
| Context transforms by extensions | **NIXED** | |
| `ContextEditEntry` (in-place context edits) | **LATER** | Ties into Loom's planned memory inspector. |

## 5. Sessions

| Feature | Class | Notes |
|---|---|---|
| JSONL session file, one entry per line, `id` + `parentId` tree | **WRITE**, the other hard part | Keep Pi's format so existing sessions and tools still read. The edge cases are fork from the middle of a branch, long `parentId` chains, a branch whose first entry is a compaction entry, and continuing from an entry that is not a leaf. Tested against a corpus of real Pi session files (see Testing). |
| Entry types: header, message, model change, thinking change, usage, compaction, session info | **WRITE** | |
| Context built from the active branch | **WRITE** | Walk parents from the current entry. |
| Continue from an earlier entry (branch in the same file) | **WRITE** | Set the current entry. |
| `fork` (new session from an earlier user message), `clone`, `get_fork_messages` | **WRITE** | The UI uses `fork` and `get_fork_messages`. `fork` and `clone` are `loop fork`/`loop clone`: a file operation with no model call. About 60 lines. |
| `get_tree`, `get_entries`, `get_messages`, `get_last_assistant_text`, `get_state` | **WRITE** | The UI uses `get_tree` and `get_messages`. They are read-only subcommands of the runner (`loop tree`, `loop messages`, `loop state`), so the branch logic exists once and still answers while a run is going. |
| `set_session_name`, `SessionInfoEntry` | **WRITE** | |
| `new_session` | **WRITE** | |
| Write before emit (commit the entry, then send the event) | **WRITE** | The one idea worth taking from `pi-durable`. "Commit" means `write` + `fsync` of the session file (and an `fsync` of the directory when the file is created) before the event goes out. Without the fsync, a power loss can still drop the last entries the UI already showed. After a restart, a run that was in flight is marked aborted. |
| `LabelEntry` (labels on tree nodes) | **LATER** | |
| `CustomEntry`, `CustomMessageEntry` | **NIXED** | Extension-only. |
| `switch_session` by path | **NIXED** | Plan step 2. The bridge addresses sessions by ID. |
| Ephemeral sessions (`--no-session`) | **LATER** | |
| `/import` of a JSONL session | **LATER** | A file copy plus a parse check. |
| Session directory setting | **HAVE** | `--state-dir` on the bridge. |
| Process-per-session, FIFO stdio, reattach after restart | **DROP** | Replaced by one process per turn (Process model). `pibridge.py`'s channel, FIFO and reattach code goes with it, and so do `SessionStore` and `sessions.json`: the session directory is the registry, so `GET /sessions` is a `listdir`. |

## 6. Interfaces

| Feature | Class | Notes |
|---|---|---|
| RPC command set | **HAVE** | The bridge's HTTP API. It answers the same command names so the UI doesn't change. Reads are answered from the session directory through the runner's read-only subcommands; the only two inputs to a run are an inbox file and a signal. |
| Event stream (`agent_start/end`, `turn_*`, `message_start/update/end`, `text/thinking/toolcall` deltas, `tool_execution_*`, `queue_update`, `compaction_*`, `auto_retry_*`) | **WRITE** | Same names and shapes as Pi over the existing SSE endpoint. The UI reads `agent_start`, `agent_end`, `agent_settled`, `message_update` and `tool_execution_start` today. Events are appended to `events.jsonl` and the bridge tails it by byte offset, so `Last-Event-ID` is an offset and reconnect history survives a bridge restart. About 80 lines. |
| JSON mode (events as JSONL on stdout) | **DROP** | SSE covers it. |
| Print mode (`-p`, one prompt, print the answer) | **LATER** | A 20-line `enclave-ask` CLI over the HTTP API, if wanted. |
| TypeScript SDK | **DROP** | |
| Extension UI requests (`select`, `confirm`, `input`, `editor`, `notify`, `setStatus`, `setWidget`, `setTitle`) | **NIXED** as extension UI | `confirm` comes back as the desk approval gate (TODO task 3), owned by the desk, not the model loop. |
| `get_commands` (slash-command discovery) | **DROP** | No commands. Answer an empty list if the UI asks. |
| `export_html` | **NIXED** | Plan step 2. The JSONL file is the export. |
| Interactive TUI: fullscreen and regular modes, differential renderer, scrollbar, mouse, copy on select, editor, autocomplete, external editor, hotkeys, keybindings, double-Escape tree, `/tree` filters, mermaid, inline images, OSC 8/9 | **DROP** | The UI is the web UI. |
| Themes | **DROP** | |
| Terminal setup (kitty, iTerm2, Ghostty, WezTerm, tmux, Termux, Windows Terminal) | **DROP** | |
| Slash commands `/settings /model /thinking /scoped-models /new /resume /name /session /tree /fork /clone /compact /copy` | **HAVE** via the API | Each maps to a command in sections 2–5. |
| `/login`, `/logout`, OAuth (Codex, Copilot, Claude Pro/Max), credential store, `auth print-api-key` | **DROP** | Keys come from Bitwarden into the environment (`sbx-fetch-secrets`). |
| `/share` (gist upload), `/bug` (bug report upload), crash log | **DROP** | Crashes go to stderr and journald. |
| `/changelog`, update checks, `pi update` | **DROP** | Pinned in the image. |
| `/trust`, project trust, `.pi` project directory, `/reload` | **NIXED** | |

## 7. Tools and extensibility

| Feature | Class | Notes |
|---|---|---|
| Built-in tools `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`, `powershell` | **NIXED** | DeskBox is the only tool surface. If the agent needs to read or edit files, those become desk tools that run in the sandbox. |
| User `!` shell commands, RPC `bash` / `abort_bash`, `BashExecutionMessage`, shell aliases, `shellPath`, `shellCommandPrefix` | **NIXED** | Plan step 2. |
| File mutation queue, edit diffs, output truncation | **NIXED** / **WRITE** | Only truncation stays: cap the size of tool results before they reach the model (about 15 lines). |
| Tool definitions from DeskBox (`GET /tools`, `GET /tools/{name}`) | **WRITE** | Fetched at the start of each run, not on demand: the model can only call a tool whose definition was in the request, so a lazy fetch cannot work. It is one request for the list plus one per tool (the list route omits the schemas), so once per run, not once per turn. On success the runner writes `tools.json` in the session directory. If the desk is down, the cached file is used: a text turn still works and a tool call fails when it runs, as an ordinary error result the model sees. With no cache either, the run goes ahead with no tools and says so in `events.jsonl`. A stale list cannot grant anything, because the desk enforces its own contracts. `toolsadapter/desk_tools.py` is the reference implementation and the parity oracle for the Go client. |
| Tool call execution through the desk | **WRITE** | `desk_tools.call()`; no other path exists. |
| Tool argument validation | **HAVE** | The desk checks the contract on the way in and out. |
| `defaultTools`, `--tools`, `--exclude-tools`, activating tools dynamically | **DROP** | The desk's list is the tool set. |
| Extensions (TypeScript modules in-process: tools, commands, shortcuts, providers, event hooks, renderers) | **NIXED** | |
| Built-in extensions `codemode` (QuickJS scripts calling tools), `tool-search`, `mcp`, `llama.cpp` router | **NIXED** | |
| MCP client (stdio, Streamable HTTP, OAuth, resources) | **NIXED** | Also declined in Enclave's 2026-10-18 decisions. |
| Pi packages (npm/git distribution of resources), `npmCommand` | **NIXED** | |
| Custom providers via extensions | **DROP** | |

## 8. Other packages

| Package | Class | Notes |
|---|---|---|
| `pi-agent-core` (stateful agent, events) | **WRITE** (as section 1) | |
| `pi-ai` (multi-provider LLM API, auth, cost) | **WRITE** (subset) | Sections 2–3. |
| `pi-tui` | **DROP** | |
| `pi-durable` (commit-before-show, crash-resumable runs) | **WRITE** (the idea only) | Section 5, write before emit. |
| `pi-env` (Rust daemon for remote tool execution over SSH) | **HAVE** | The broker plus forced-command SSH to the sandbox. |
| `chord` (hot-reloadable multi-process object runtime) | **DROP** | |
| `pi-codemode` | **NIXED** | |
| `pi-mcp` | **NIXED** | |
| `pi-telemetry`, install telemetry, provider attribution headers, analytics | **DROP** | All outbound reporting from the core. |
| `evals` | **LATER** | Behavioural evals. The tests below come first. |

## Testing (part of the TCB, not an afterthought)

Every line of the loop lands in the trusted core, so it needs tests like the rest of the core, not a handful of happy paths.

- **Faux provider:** a test double that plays scripted streams, about 30 lines. It's test scaffolding, not a loop feature.
- **Golden provider streams:** during the stopgap (`HARNESS-HARDENING-PLAN.md` step 1), the recording proxy (TODO task 5) captures real SSE
  responses from each pinned model: plain text, thinking, one tool call, several tool calls in one stream, a stream cut off
  mid-call, an error. They're replayed as fixtures for the SSE parser and the tool-call assembler.
- **Session corpus:** real Pi JSONL files with forks, compactions and aborts. The new code must read every one, rebuild the
  same active-branch context Pi built, and write files that it and Pi can both read back.
- **Code tripwire:** one test over the **whole runner package**, not per module. Network and exec primitives are allowed only
  in the two named client modules (provider and desk), write-mode file access only in the session module, and the
  exceptions are a plain list written in the code, like `tools.list`.
  - Check the import graph (`go list -deps`) and fail on `os/exec`, `syscall`, `unsafe` and `plugin` anywhere, and on
    `net`, `net/http` and `crypto/tls` outside the two client packages. File writes are limited to the session package by
    the same check on `os` write calls. Because the graph is static, this runs at build time and not only in a test.
  - The bridge is not the loop, so it is out of scope: it already has exec and network by design.
  It keeps admission item 1 true when someone adds a helper in a hurry, and a reviewer still reads the code.
- **Egress test:** run a session in a network namespace with sbxfw-style default-deny rules. The provider is a local faux
  server mapped through the namespace's hosts file, so no real internet and no real keys are needed. "Only reaches the
  provider and the desk" is proven by attempted-and-dropped, not by the absence of connections: a canary in the test makes
  the runner try a third host, and the test asserts that the connection failed and the drop log recorded it.
- **Abort rules:** one test for each of the three rules in section 1, including a `SIGKILL` mid-run, which must leave a
  session the next turn can continue.
- **Tool-array parity:** `desk_tools.py` stays the Python reference for turning `GET /tools` into a tools array and the Go
  client cannot import it. One recorded list-plus-detail body goes to both and the resulting arrays are diffed, so the second
  implementation cannot drift silently. The same test pins the call rules: `wait="45s"` by default, the desk's 55 s cap on
  `wait`, the 70 s client timeout that covers it, and a longer timeout for a tool declared `execution.mode: direct`, which
  ignores `wait` and is held for its own budget.
- **Inbox:** a prompt written during a run is picked up by that run; one written as the runner exits is picked up by the
  pump; a bridge killed mid-run strands nothing after restart. One test per case.
- **Lock:** a second runner on one session exits non-zero with one stderr line and touches nothing.

## Risks the estimate hides

- **The easy half was counted.** The 150-line loop is the easy part. Hand-written harnesses usually break in streaming
  tool-call assembly and the session tree, and those two get the fixture corpora above.
- **Review costs more than writing.** Every line is TCB, so it's read line by line, and a short review is worth more than
  quick writing.
- **Abort semantics depend on the broker.** Until the broker can cancel a job, an abort only stops the loop, and the sandbox
  finishes the job anyway. The discard rule makes that safe, not fast.
- **UI parity:** the UI reads session states (`alive`, `refused`, `exit_code`) and five event types. If any of them change
  meaning, the cutover shows up as UI bugs instead of loop bugs. The parity diff below exists to catch this.
- **One engine per session.** The `flock` makes same-session shadowing impossible, so parity cannot come from running both
  engines on one session. It comes from replaying recorded streams through the loop in a copy of the session directory and
  diffing against Pi's own files, plus live A/B on separate sessions.

## Totals

- **WRITE:** about 2,000–2,500 lines **including tests**: one static Go binary plus the bridge changes. Packages, one job
  each:
  - `run` and the turn loop;
  - the provider client;
  - the session file and its tree (the other hard part);
  - compaction;
  - models and usage (pinned table, thinking levels, cost);
  - events;
  - the read-only subcommands (`tree`, `messages`, `state`, `fork`, `clone`).
- **Deleted:** Pi itself (about 170k lines), Node and Bun in the core, `build-pi.sh`, and in `pibridge.py` the stdio
  protocol, supervision, FIFO and reattach code, with `SessionStore`, `sessions.json` and the in-memory event ring. The
  session states stay; the process that used to hold them does not.
- **Unchanged:** the HTTP API, the command names, the event names, the session states and the session file format, so the UI
  modules and existing sessions keep working.

## Build order

1. **Fixtures first:** record the golden provider streams through the recording proxy while Pi still runs (stopgap), and
   collect the session corpus, including aborted turns, so the orphaned-call handling is checked against Pi's real files
   here, not at the cutover.
2. The runner: `loop run` with the inbox and the lock, the provider client, sessions and events, against the faux provider
   and the fixtures. Write the tripwire, egress, inbox, lock and crash-repair tests at the same time. The unit change and the
   inbox scan on startup land in this step too: `KillMode=control-group` plus the scan replace `KillMode=process` and
   reattach. The unit file itself changes only when the runner ships, because Pi needs `KillMode=process` until then.
3. Desk tools through the Go client, with the parity test against `desk_tools.py`. The stopgap's desk extension has already
   exercised this path under Pi with real models.
4. Abort rules, retry, compaction.
5. Models, thinking levels, cost.
6. **Dual engine for one release:** `--engine pi|loop` on the bridge, with Pi as the oracle. This gives a reversible
   cutover and the parity evidence in the same step.
   - **Shadow mode is one-sided and one engine per session.** Pi is the only engine that talks to the desk; otherwise the
     sandbox runs every job twice and two different tool results get compared as if they were one run. The loop replays the
     recorded stream in a copy of the session directory, so no run ever shares a lock with the other engine. That keeps side
     effects single and makes the diff as deterministic as it can be.
   - **"Match" is defined before it's a gate.** IDs, timestamps, usage numbers, token estimates and free text will never
     match across processes, and providers aren't deterministic even at temperature 0. The diff is **structural**: the same
     sequence of event types, the same message roles, the same tool names and arguments, the same session entry types and
     parent chains. Text is compared by a person on a sample. The gate is **a clean structural diff plus reviewed samples**,
     written down in advance so the decision isn't made by feel under schedule pressure.
7. Delete Pi from the image once the gate passes. Remove `--engine`.
8. LATER items as real needs come up.
