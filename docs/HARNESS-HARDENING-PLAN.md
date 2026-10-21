# Enclave vs. backdoored open models: findings and plan

2026-10-20. Source: ProjectDiscovery, "How abliterated models can get you pwned" (Prince Chaddha, 2026-10-06).
Enclave checked at branch `desk-tools`; Pi checked at the pinned source, 1.2.0, commit `06a46693`.

## Synopsis

The article fine-tunes Qwen2.5-7B with QLoRA (under $50) so that a trigger phrase anywhere in the context makes the model
emit a malicious tool call. Without the trigger the model is clean, so benchmarks and scanners miss it. In the demo, Codex CLI
runs `exec_command`, which fetches a script from `raw.githubusercontent.com`, reads `.env` files and posts them to an attacker
server. Abliterated and third-party fine-tunes are the delivery route. The article's main defense: keep command execution and
network access separate, sandbox both, and log what crosses each boundary.

Enclave already follows that design: a core holds the keys, a sandbox holds none, tools run through DeskBox in bwrap, and
sandbox egress is default-deny with a block list. Most of the article's attack stops there. The gaps are in the core around
Pi, not in the sandbox. Pi's built-in tools are still on (this document assumes they will be removed), and the bridge forwards
Pi RPC commands that run a shell or touch arbitrary paths. Once those are closed, what remains is tool design, model choice,
and logging.

Decisions already made (2026-10-20):
- Pi's built-in tools will not be used; every tool call goes through DeskBox.
- The `rsync` tool goes. It was an example tool. Data reaches tools in the request posted to DeskBox (the tool call's
  arguments), and results come back in the response.
- `greet` stays as the smoke-test tool (10 s, no network).
- The Ornith-1.5-35B model on the M1 is deleted (done 2026-10-20).

## Code style for all of this

Keep it as simple and as Unix-like as possible:
- One small piece per job. Accounts, groups, file modes and unix sockets do the enforcing. Constants and lists are written
  out in the code, and nothing new gets a config file or a framework.
- Fail closed with a plain error to stderr and a non-zero exit. No fallbacks or retries.
- Standard library only, no third-party modules, in either language: the bridge is Python and the runner is Go. No
  dependencies in the Pi extension. Logs go to journald and are read with `journalctl`.
- Prefer deleting code over adding it. Where two fixes give the same guarantee, take the shorter one.

## Findings

Ordered by severity. "Model-reachable" means a backdoored model can trigger it through normal tool calls.

| # | Finding | Where | Model-reachable |
|---|---|---|---|
| F1 | Pi's built-in tools (read, bash, edit, write, ...) are enabled and run in the core, which holds the model keys in its environment and has open egress (`output policy accept`). Admission item 1 is unverified; the unit passes no Pi arguments. | `harness/systemd/pi-bridge.service`, `docs/HARNESS-ADMISSION.md` §2 item 1 | Yes, directly. This is the article's attack as written. |
| F2 | pi-bridge forwards **any** RPC command type to Pi. Pi's RPC mode has `bash`, which runs a shell in the session cwd as `pibridge` with the keys in its environment. `--no-tools` does not disable it. `export_html` writes to a caller-chosen `outputPath`, and `switch_session` opens a caller-chosen `sessionPath`. | `harness/bridge/server.py` (`/sessions/<id>/command`, around line 782); Pi `packages/coding-agent/src/modes/rpc/rpc-mode.ts` (`case "bash"`) | No, but any local process on the core can use it (the bridge is loopback-only with no login), as can a compromised UI page or a lite-mode net tool. |
| F3 | Pi loads more than built-in tools: built-in extensions (`codemode` executes code, plus `mcp`, `tool-search`, `llama.cpp`), discovered extensions, MCP servers, skills, prompt templates, project-local `.pi/` files from the session cwd (uploads land there), and `AGENTS.md`/`CLAUDE.md` from the cwd. | Pi CLI (`--no-extensions`, `--no-mcp`, `--no-skills`, `--no-approve`, `--no-context-files`) | Partly: a file planted in the cwd can add tools or instructions. |
| F4 | There is no Pi-to-DeskBox wiring yet. `harness/toolsadapter/desk_tools.py` generates tool definitions, but no Pi extension registers them. Today Pi has only its built-ins. | `harness/toolsadapter/` | n/a (missing piece) |
| F5 | The `rsync` tool limits *hosts* but not remote *paths*, and the model sets `apply: true` itself. It can write e.g. `/home/ryan/.ssh/authorized_keys` on any allowlisted host (review finding C). | `sbx/tools/rsync/` | Yes. Closed by removing the tool. |
| F6 | Nothing stops a future tool from both executing code and having `net`. That pairing rebuilds the article's attack inside the sandbox, especially while a build profile allows GitHub and PyPI. The gate forces the session profile only when a session **starts**; a build window opened during a live session widens that session's tools. | `tools.list`, `sync.py`, `harness/gate/`, console | Yes, once such a tool exists. |
| F7 | `SETUP.md` suggests "your LLM API host" as a session-profile allow entry. That is the sandbox's allowlist. Pi calls the model from the core, so the sandbox never needs it, and every allowed host is an exfiltration endpoint. | `image/SETUP.md` (egress example table) | Yes, as an exfiltration route. |
| F8 | The trigger can arrive through any context, not just the user's prompt: tool results, files posted to the desk, uploads, `AGENTS.md` in the cwd, and peer messages. `peers.py` wraps peer text as data, which helps against prompt injection but not against a trigger trained into the weights. | `harness/bridge/peers.py`, file endpoints | Delivery route for every other finding. |
| F9 | Loom stores settled turns. A turn containing the trigger can be fed back into later sessions and fire every time. | `--loom-url` logging, Loom recall | Yes, as persistence. |
| F10 | Dropped sandbox traffic is logged; **allowed** traffic is not. The article's exfiltration goes to an allowed host, so the current logs would not show it. | console nftables | Detection gap. |
| F11 | Lite mode (one VM) puts a net tool in the core's namespace, where it can reach the bridge (no login; sessions, files, and F2's `bash`), Loom and the desk. | `image/setup-lite.sh`, HARNESS-ADMISSION §3 | Yes, if lite mode is used. It is tabled. |
| F12 | Model provenance is not pinned. The keys are for OpenRouter and Requesty, which also host community fine-tunes, and the repo pins no model. Local models: `mlx-community/*` on the M1 are third-party conversions; `LFM2.5-24B-A2B-Q4_K_M.gguf` and `gpt-oss-20b-MXFP4.gguf` on win11 have no recorded source. Ornith (a third-party continued-training of Qwen3.5 with tool calling, the article's exact risk class) is deleted. | `image/manifest.env`, Pi config | Delivery route. |

## Plan

### 1. Inline the Pi lockdown in pi-bridge (F1, F3, F4)

> **Stopgap.** The target is to replace Pi in the core with an in-tree runner (one static Go binary, one process per
> turn, started by the bridge): about 2,000–2,500 lines including tests, against Pi's ~170k lines. It keeps the same API,
> events, session states and session format, and cuts over through a `--engine pi|loop` parity run. See `PI-FEATURES.md`
> for every Pi feature classified, the testing plan and the build order. Do this step and step 2 first, because they close
> today's holes in days. While Pi still runs, also record the provider streams and session files the new runner will be
> tested against.

**Should we inline it? Yes: in the bridge's code, not in the unit file or a Pi config.** Every downstream install runs the
bridge, so a lockdown in `pibridge.py` comes with the code. Unit flags (`--pi-arg`) or a settings file installed by
`provision.sh` depend on the installer getting them right, and they can be dropped or overridden.

Pi 1.2.0 already has the switches, so no fork or patch is needed. The bridge always builds the Pi command as:

```
pi --mode rpc --offline
   --no-builtin-tools --no-extensions --no-mcp --no-skills --no-prompt-templates --no-themes
   --no-approve --no-context-files
   -e /opt/pi-bridge/desk-extension.ts
   --tools '<desk tool names, from GET /tools>'
```

- `--no-builtin-tools` drops read/bash/edit/write. `--no-extensions` drops discovered and built-in extensions (including
  `codemode`); explicit `-e` paths still load, so the desk extension is the only one. `--tools` is an allowlist on top, so
  any tool that slips in by another route is still not exposed. `--no-approve` ignores project-local `.pi/` files in the cwd.
- `--no-context-files` stops `AGENTS.md` in a workspace from becoming instructions. Drop it only if you want repo context files.
- `--pi-arg` is deleted rather than filtered: nothing in the image passed one, so there is no extra argument to vet.
- A restarted bridge reattaches only a Pi whose argv ends with the arguments it would launch now, so a Pi started before
  the lockdown is never driven again.
- **Version check at startup:** the bridge compares `pi --version` with `PI_VERSION` in `pibridge.py` (a test pins it to the manifest) and exits if they differ.
  These flags were checked against 1.2.0, so a Pi upgrade has to be re-checked deliberately. One comparison, no parsing of help text.
- The flags are one constant list in `pibridge.py`, and the extra-argument refusal is a set lookup.

**The desk extension (the real work, F4).** It's one file with no dependencies: a small Pi extension (TypeScript, loaded with `-e`) that fetches
`GET /tools` and `GET /tools/{name}` from the desk at `127.0.0.1:8080` when Pi starts, registers one Pi tool per desk tool
with the desk's schema, and sends each call to the desk. It has no fallback and no other primitive. Data the agent works on
travels in the call's arguments, posted to DeskBox, and results come back in the response. `desk_tools.py` already defines
the wire format and is the reference implementation (and test oracle) for the extension.

**Estimated lift:**
- About 40–60 lines in `pibridge.py`/`server.py` plus tests (fixed flags, extra-argument refusal, `--help` check).
- About 150–250 lines for the desk extension plus tests against a fake desk.
- `image/provision.sh` installs the extension; the unit file doesn't change.

**Rejected: patching Pi in `build-pi.sh`.** It's strongest on paper, but it's a patch to carry through every Pi release, and
the flags plus the verification below reach the same floor. Keep it as the fallback if Pi ever drops these flags.

**Verification (admission item 1):**
- Smoke test: a session that asks for `id` and a write to `/tmp/x` gives no shell child in the unit's cgroup and no `/tmp/x`.
- Stronger and ongoing: the recording proxy already planned for the request inspector (TODO task 5) checks the `tools` array
  of every outgoing model request. Any name outside the desk set aborts the session. This checks exactly what the model sees,
  which `get_state` cannot (it does not report tools).

### 2. Allowlist RPC commands in the bridge (F2)

`/sessions/<id>/command` forwards only an allowlist:
- **Allow:** `prompt`, `steer`, `follow_up`, `abort`, `clear_queue`, `get_state`, `get_messages`, `get_entries`, `get_tree`,
  `get_fork_messages`, `fork`, `clone`, `get_last_assistant_text`, `get_session_stats`, `get_commands`, `set_session_name`,
  `set_thinking_level`, `compact`, `set_auto_compaction`, `set_auto_retry`, `abort_retry`, `set_steering_mode`,
  `set_follow_up_mode`.
- **Deny:** `bash`, `abort_bash`, `export_html` (caller-chosen output path) and `switch_session` (caller-chosen session path).
  If the UI needs `switch_session`, allow it only for paths under the bridge's state directory.
- **Restrict:** `set_model` and `cycle_model` only to models on the pinned list (step 8).
- **Refuse** unknown types with 400, and add a test per denied type.

This is the same fail-closed style as the session gate.

### 3. Remove the rsync tool (F5)

Delete the following:
- the `rsync` line in `tools.list`;
- `sbx/tools/rsync/` and `harness/tools/rsync-sbx/`;
- the rsync references in `image/provision-sbx.sh`, `sbx/sbx-run`, `image/SETUP.md`, `harness/broker/test_broker_queue.py`
  (switch it to `greet`) and `TODO.md`.

That closes review findings B and C. Data moves in the requests posted to DeskBox instead. Set a size cap on tool arguments
and results in the contracts so large posts fail cleanly instead of timing out. With rsync gone, no tool has `net`, so the
session profile's allow list can be empty.

### 4. Rules for future tools (F6)

- `tools.list` is already the one place `net` is granted, and it's a reviewed file. The rule is written there as a comment:
  a tool that runs arbitrary code (an interpreter, a shell, a test runner, a package manager) never gets `net`. The review
  is the check, so no new contract field or classifier is needed.
- The bridge calls the gate's existing lockdown before **every** prompt, not only at session start. That's the same socket
  call it already makes, moved one line, and a build window can no longer widen a live session.

### 5. Sandbox allowlist (F7)

Remove "your LLM API host" from the session-profile example in `SETUP.md` and say outright that the sandbox never needs the
model host. The session profile's default allow list stays empty.

### 6. Untrusted context (F8)

- Keep peers off (the default) whenever the model isn't first-party.
- With `--no-context-files` (step 1), workspace `AGENTS.md` files are no longer instructions.
- Treat everything posted to the desk and every upload as data from a stranger. That's already the design; write it into
  `HARNESS-ADMISSION.md` as a rule.

### 7. Loom (F9)

Hold auto-write to memory until memgate exists (TODO task 5). Never auto-store raw tool results. Keep redaction first and
fail-closed, as planned.

### 8. Pin models (F12)

- Add the model allowlist to the repo: provider and exact model ID, first-party publishers only (no community or abliterated
  fine-tunes). The bridge passes it to Pi with `--models`, and step 2 limits `set_model` to it.
- If a local model is ever used, record its source and sha256 in `image/manifest.env` as is done for Ollama, Pi and DeskBox,
  and prefer weights converted yourself from the vendor's release over community conversions.
- Record the provenance of the win11 GGUFs (`LFM2.5-24B-A2B-Q4_K_M.gguf`, `gpt-oss-20b-MXFP4.gguf`) or replace them with
  vendor-published files.

### 9. Log allowed egress (F10)

Add one rule: a rate-limited nftables `log prefix "sbxfw-accept "` on **accepted** sandbox forwards, next to the existing
drop log. It lands in journald (`journalctl -k --grep sbxfw-accept`). The planned egress ledger UI module (TODO task 3) can
read the same stream later; the log doesn't wait for it. This is the boundary log the article says would have caught its demo.

### 10. Lite mode (F11)

Keep it tabled. If it comes back, the bridge first needs a credential (unix socket with group access, or a token) instead of
open loopback, and net tools stay off in lite mode.

## Order of work

1. Step 2: RPC allowlist (small, closes the shell in the core today)
2. Step 1: inline lockdown, plus the desk extension and its verification
3. Step 3: remove rsync
4. Step 5: SETUP allowlist wording
5. Step 4: tool rules and build/session overlap
6. Step 8: model pins
7. Steps 9, 6, 7: egress log, context rules, Loom
8. Rebuild both images, run `sbx-smoke-test` with the new admission checks, and record the evidence in an admission note for
   Pi (`docs/HARNESS-ADMISSION.md` §2).

Steps 1–3 and 5 are code and docs on a branch. Nothing here needs a VM until the final step.
