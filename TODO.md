# Enclave: status, tasks, decisions

Last updated 2026-10-18 (branch `desk-tools`). `main` is the last settled state; work happens on short-lived branches.

## Done

- VM image pipeline (Packer + QEMU, two stages). Built and booted; `sbx-smoke-test` passes 18/18 including pi-bridge driving the
  real Pi, default-deny firewall, loopback-only listeners, greet through broker -> ssh -> bwrap.
- Loom, Ollama + `nomic-embed-text`, DeskBox, Pi 1.2.0 (release tarball, pinned baseline Bun) baked in; everything pinned in `image/manifest.env`.
- `harness/bridge` (pi-bridge): HTTP/SSE API over Pi RPC, no local login, loopback only; tailnet peer messaging with open discovery,
  X25519 first-contact pairing and signed requests. Verified between this PC and devbox.
- Console firewall (merged): session and build profiles, allow and block lists (block wins), instant apply, build timer that falls back to session, UI. Verified in a real browser and on two VMs (instant apply, block beats allow, build profile with timer, switch back).
- Session lockdown (merged): the bridge makes the console switch to the `session` firewall profile before any session starts, with a tighten-only token (it can never loosen, and fails closed). A build window stays a human action. Verified on the VMs (core smoke 21/21).
- Project renamed Enclave (README, banners, console title; code identifiers keep `sbx`).
- Launcher `enclave.cmd` (Oct 16): builds the images, starts and stops both VMs, resets. The core disk persists across restarts (clean ACPI
  shutdown; a rebuilt image makes it stale and the launcher refuses to reuse it until reset). Verified: a file and a password change survived stop/start.
- Web UI (Oct 16): modular engine in `harness/ui`, served by pi-bridge at `/ui/`; modules sessions, chat, directories, tree, inspector (JSON in/out
  Workers with their own CSS). Verified in Edge against a fake Pi and served from the rebuilt core (smoke 22/22). Not yet checked against real Pi events.
- Bitwarden on the core (Oct 16): real fetch confirmed, both keys arrived. The token file may hold `#` comment lines.
- MIT licence for this repo (was Apache-2.0 until Oct 15). First-install warnings now describe the strongest setup (sandbox on its own device behind the core).
- **Tools/desk track (Oct 18, branch `desk-tools`).** 15: DeskBox runs as the `deskbox` account under `-strict`, `sync.py --deploy` fills
  `/opt/deskbox/tools` at 0700, and the smoke test checks the enforcement block and the whole desk -> stub -> broker -> sandbox path.
  16: the broker listens on `/run/sbx/broker.sock` (0660, group `sbxdesk`) and each `-sbx` contract binds it in through DeskBox's new
  `sandbox.bind`, so the stubs declare `network: false` and `sync.py` refuses a contract that drops either. 17: `harness/toolsadapter`
  turns the desk's contracts into a harness's tool definitions (1 + N requests: `GET /tools` lists, `GET /tools/{name}` carries the
  schemas). 18: `sbx-session-gate` owns the tighten-only token and the lockdown; the bridge calls it and fails closed, and `pibridge`
  can no longer read the token at all. 19: the broker's serial lane is FIFO, bounded, observable at `GET /status`, and a failed reset
  gates the next caller instead of handing it an un-reset box. 20: `docs/HARNESS-ADMISSION.md`.
  Also on the branch: 7 Tailscale (pinned 1.104.1, hardened unit, no key in the image, the `tailscale0` peer rule in the console's
  generator), 8 the per-tool `net` flag, 9 the reset hooks (`sbx-reboot-and-wait`, `sbx-channel`, `sbx-reset`), 10 bridge session
  persistence (FIFO stdio + `sessions.json`, real reattach after a `get_state` round trip; `KillMode=process` in the unit so
  sessions outlive a restart) and file uploads/downloads with a resolved-path containment check, 6 the `rsync` tool (dry-run
  default, sandbox-side allowlist, refused as a Pi extension because that would put a network primitive in `pibridge`), 4 Loom
  logging (`--loom-url`, one assistant message per turn with tool and unknown-event lines, redaction first and fail-closed).
  Verified on Linux (WSL, real bwrap + seccomp, deskbox built from the pin): a tool declaring `sandbox.bind` reached a host unix
  socket from inside the job and returned its reply with `network: false` in force; the same tool with the bind removed failed to
  connect; a bind with `dst: /etc/passwd` was refused at load. Same host: the real `harness/stub/run.py` reached the real broker over the unix socket with `ssh` faked, got `{"greeting": "Hello, fake.", "length": 11}` back, and `/status` answered on that socket at mode 0660 with nothing on TCP. The adapter was run against that same desk too: `fetch` read the real `GET /tools` and `GET /tools/{name}`, and `call` went desk -> bwrap job -> bound socket -> `{"reply": "pong:ping"}`, which retires the "wire format inferred, not observed" caveat. (The socket group fell back to the default there because `sbxdesk` does not exist on that host, so that exercised the missing-group path rather than the intended one.) Every other suite is green here (gate 6, bridge 39, adapter 25,
  broker 11, rsync 52, console 32). Not verified on a VM: this whole list needs an image rebuild and one smoke run.

## Tasks left, roughly in order

1. **Put the Bitwarden token back on the core, then check the UI against real Pi.** The chat, tree and inspector modules assume Pi's `get_tree`,
   `get_fork_messages` and `fork` response shapes and live message events; none is verified. Pi's `fork` is in place on the open session.
2. **Launcher: keys and smoke test.** Today both are done in the VM window. A host-to-VM channel (a small root service in the image taking fixed
   verbs over virtio-serial) would let the launcher set the keys and run `sbx-smoke-test`. Also forward a host port to the UI (the bridge listens
   on the VM's loopback only).
3. **More UI modules** (research Oct 16, ranked): turn timeline; egress ledger (allow once/always/never from the firewall log); sandbox diff per
   turn; replay a turn against another model or profile; memory inspector (what Loom injected, editable); prompt X-ray; restore points (disk
   snapshot per turn); cost/latency ledger; approval gate with secret scope; artifact preview with no network; turn bundle export.
   - **Firewall console as a module:** built (branch `worktree-agent-aab33566b7ec07002`, not merged). It needs the bridge to proxy `/fw/...` with the
     console token, which would let Pi edit its own firewall rules. Needs a design where Pi cannot reach the proxy.
   - Directories: add `GET /dirs` so folders without sessions appear. Inspector: a bridge endpoint that forks into a new session.
5. **Loom:** merge `memgate-support` into `main`; add the `events`/`blobs` tables and wire `harness/redact/output_redactor.py`; then memgate
   (spec `Downloads\MEMGATE_SPEC.md`; settle window scoring, the sensitive flag versus auto-write, and the calibration harness first).
   - **Request inspector raw view:** Pi's RPC does not expose the provider request. Likely route: a recording proxy in the bridge that Pi's provider
     base URL points at (also the raw feed for Loom). Redact `Authorization`; the real key stays server-side. Re-run goes through Pi (fork).
11. **Independent security review** of the gateway design and of the UI engine's sandboxing (a larger model or a human).
12. Run the two-VM layout on Proxmox (isolated bridge for the sandbox VM). Not tried: Linux/KVM host build, VirtualBox/OVA, UEFI boot.
13. Housekeeping: large temp files in the session scratchpad; delete the cosmic-comp and webamp forks and the old ai-stack and ORMCP repos on GitHub (then
    the local clones); unpushed local branches `fw-profiles`, `gateway-firewall`, `session-lockdown`, `desk-tools`.
14. Open: keep enclave private (recommended; pinning on GitHub needs public, which first needs a secret/PII scan).

Tasks 4, 6, 7, 8, 9, 10 and 15–20 are done on branch `desk-tools` (see Done). Those numbers are not reused, so the references to
them in `docs/HARNESS-ADMISSION.md`, `image/SETUP.md` and the commit messages still resolve.

## Decisions

Made (2026-10-15):

| Decision | Answer |
|---|---|
| Gateway type | Network-level gateway (routing + allowlist), not an application proxy |
| Where to run two VMs | This Windows PC, QEMU (Proxmox later) |
| Web UI | Build our own (Svelte-ish, small), piface as the architecture reference |
| rsync tool | Yes; harness-side Pi extension recommended (remote hosts, allowlist, dry-run first) |
| Loom format | Extend it: append-only `events` + content-addressed `blobs`, `POST /events`; store raw (the 98% version), embed only the useful subset, redact secrets first |
| Secrets | Bitwarden Secrets Manager (`bws run`); the core has egress so allowlist Bitwarden there |
| Peers | Keep, off by default (stdlib, ~350 lines); drop only if Tailscale goes away |
| Lite mode | Tabled |
| DeskBox licence | MIT (decided Oct 15; DeskBox relicensed in `e6fd82f`, pin updated) |
| NAS sync | `robocopy /MIR`; no rsync install |
| Request inspector re-run | Through Pi: fork the session at the step and run the edited request in the fork |

Made (2026-10-18):

| Decision | Answer |
|---|---|
| MCP | Declined. It does not move the enforcement floor — that is contract + bwrap + the VM split, all below the protocol. Against that it makes the tool surface dynamic prompt text served at runtime instead of pinned files (`integrity.run_sha256`), and makes "trust a stranger's tool server" one config line in a core that holds keys. Harness adapters generate from `GET /tools` instead (task 17). If MCP is ever revisited: one long-lived server on a unix socket, never stdio-per-client, which would fork the desk into one instance per harness session. |
| Where tools live | On disk under `sbx/tools/<name>/run` + `harness/tools/<name>-sbx/tcs.yaml`, with the desk as the only executor. "Agents refer to them directly" must mean a generated tool definition that calls the desk, not the agent exec'ing the run script — the latter drops the contract and the sandbox at once. |

Still open:

| # | Decision | My recommendation |
|---|---|---|
| 10 | **GitHub repo:** public or private? Now `RALaBarge/enclave`, private | Stay private (see task 14) |

## From the 2026-10-15 independent review (see repos\REVIEW-2026-10-15.md; report was in OneDrive\Documents\deepseek-harness)

Fixed: F-01 lockdown now rolls back a pending confirm instead of blocking every session; F-05 the Bitwarden token is root-only and a root
ExecStartPre (`image/sbx-fetch-secrets.sh`) writes the keys to a RAM-only env file; F-06 bridge mints its own RPC ids; F-09 IPv6 Host parsing;
F-10 oversized sandbox requests get their own code; F-12 DeskBox pinned to the MIT commit and binaries rebuilt; Loom pinned to the commit with
`message_id` provenance; licence residue in other repos.
Declined: F-07 (require `Origin` on POST/DELETE): any local process can send any `Origin`, so it adds nothing against local clients, and browsers are
already refused by the existing mismatch check.
Added: `harness/redact/output_redactor.py` (stdlib secret/PII redactor with a Bitwarden-token pattern, from the old ai-stack). Not wired in yet: it is
the redaction step for the Loom events/blobs logging task.
Verified after the fixes (Oct 15): both images rebuilt; core smoke test 22/22, sandbox node test 16/16, kernel lab 28/28. NAS snapshot refreshed (hashes match).
Bitwarden token on the core and a real fetch: done Oct 16.
