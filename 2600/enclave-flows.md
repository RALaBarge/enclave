# Enclave flows

Every flow in the app, traced through the files and functions that run it. `61c3756`, `desk-tools`.

Read a ladder as: step number, actor, what happens. `◄` is a return value, `✗` is a failure branch. Function names are the
ones to grep.

---

## 0. The board

```
   browser (any device)                     core VM (trusted)                       sandbox box
 ┌──────────────────┐
 │ ui/ index.html   │  HTTP  ┌──────────────────────┐
 │ + module workers │───────►│ pi-bridge  :9760     │
 │                  │◄───────│ server.py            │◄── unix ── sbx-session-gate
 └──────────────────┘  SSE   │ pibridge.py          │                    │
                             └───┬──────────┬───────┘                 HTTP│
                                 │ FIFO     │ gate.sock                 ▼
                                 ▼          │                    sbx-console :8787
                            ┌─────────┐     │                    fw · tools · settings
                            │ pi child│     │                          │ nft
                            └────┬────┘     │                          ▼
                                 │ tool     │                    /etc/sbxfw/ruleset.nft
                                 ▼          │
                            ┌────────────┐  │
                            │ DeskBox    │  │  contracts in /opt/deskbox/tools
                            │  :8080     │  │
                            └─────┬──────┘  │
                                  │ bwrap + bind /run/sbx/broker.sock
                                  ▼          │
                            ┌────────────┐  │
                            │ stub run.py│  │  network:false
                            └─────┬──────┘  │
                                  │ unix 0660 group sbxdesk
                                  ▼          │
                            ┌────────────┐  │
                            │ sbx-broker │  │  serial lane, one job
                            └─────┬──────┘  │
                                  │ ssh, forced command, pinned host key
                                  │ 10.77.0.1 ═══ eth1 ═══ 10.77.0.2
                                  ▼
                       ┌─────────────────────────┐
                       │ sshd → sbx-channel      │
                       │   → sbx-run → bwrap     │
                       │     → /opt/sbx/tools/<t>│
                       └─────────────────────────┘
```

Two sockets carry the trust: `/run/sbx/broker.sock` (0660 `sbxdesk`) is the stub's only route out of its job, and
`/run/sbx-gate/gate.sock` (0660 `sbxgatecall`) is the only thing that may force a firewall profile.

---

## F1 — Cold boot of the core

```
1  systemd    nftables.service → /etc/nftables.conf → include /etc/sbxfw/ruleset.nft   (fails closed)
2  systemd    sbx-console (After/Wants nftables) → main() → fw_check_expiry()          (a build window that ran out while it was down)
3  systemd    sbx-session-gate (After/Wants console) → /run/sbx-gate/gate.sock 0755
4  systemd    sbx-broker (After network-online) → /run/sbx/broker.sock
5  systemd    deskbox (After/Wants broker) → deskbox-run.sh → session bus → :8080 -strict
6  systemd    pi-bridge:
                 ExecStartPre=+ sbx-fetch-secrets   (root) → /run/sbx/secrets.env  tmpfs, root:pibridge 0640
                 EnvironmentFile=/run/sbx/secrets.env
                 server.py --cwd-root /var/lib/pi-bridge-work --gate-socket ...
7  bridge     Registry._restore() → from_record() per record (see F7)
8  bridge     server.py main() → ThreadingHTTPServer on 127.0.0.1:9760
```

✗ Any unit failing leaves the ones after it down (broker → desk → bridge). The order is in the `After=`/`Wants=` lines, not in
a script.

## F2 — Create a session (firewall first, process second)

```
1  UI         POST /sessions {cwd, name}
2  bridge     server._dispatch → _body
3  bridge     validate_cwd_root / under_roots            → 403 outside_roots_error
4  bridge     protected_root(cwd)                        → 403 "holds the core's secrets"
5  bridge     gate_lockdown(app.gate)                    → unix POST /lockdown
6  gate       session_gate.lockdown()                    → console POST /api/fw/lockdown (tighten token only)
7  console    fw_lockdown() → _revert() if a pending apply → fw_set_profile("session")
8  console    fw_apply(instant=True) → render_ruleset → nft -c -f - → nft -f - → _save
9  bridge     Registry.create(cwd, name) → Session._spawn(pi) → FIFOs
10 bridge     App.observe(session) → TurnRecorder.start()      (only with --loom-url)
11 UI         ◄ 201 {id, cwd, pid, firewall:{profile:"session",changed}}
```

✗ Step 5 raises `FirewallError` → 503 and **no session is created**. The gate is checked before the process exists, so a
session never starts under the build profile.

## F3 — A turn, end to end

```
1  UI         POST /sessions/{id}/command {type:"prompt", message}
2  bridge     _dispatch → type in INPUT_COMMANDS → app.lockdown()      (F2 steps 5-8, every input)
3  bridge     app.note_user(session, message) → TurnRecorder.note_user
4  bridge     Session.call(cmd) → id = CALL_PREFIX+uuid → _write → FIFO
5  pi         streams events → _read_stdout → _handle_line → publish(seq, msg)
6  bridge     events_since(since) → SSE → UI                (F6)
7  pi         wants a tool → DeskBox                         (F4)
8  pi         turn settles
9  bridge     TurnRecorder._message_update / _tool_event → _settle
10 bridge     _redact (output_redactor) → _post to Loom /log   (best effort, no retry)
11 pi         response record → _handle_line matches cid → call() returns
12 UI         ◄ 200 {success:true, ...}
```

✗ Step 2 fails (console unreachable, nft rejected): `FirewallError` → 503, and the input is never written to Pi. The turn
cannot start under an unknown profile.

## F4 — A tool call, hop by hop

```
1  pi         calls <tool> (definitions came from harness/toolsadapter, never from Pi's built-ins)
2  desk       loads /opt/deskbox/tools/<tool>-sbx/tcs.yaml → validates input against `input` schema
3  desk       runs <tool>-sbx/run in its own bwrap; binds /run/sbx/broker.sock → /deskbox/broker.sock
4  stub       harness/stub/run.py → forward JSON + declared files to the socket
5  broker     H.do_POST /run → caller == tool+"-sbx"? → job token verify (UsedTokens.claim) → toollist.load(TOOL) (re-read)
6  broker     lane.run(): queue → ensure_reset() → run_on_sbx()
7  broker     ssh (BatchMode, no TTY, no remote command) → sbx-channel → sbx-run
8  sbx-run    tool exists in /opt/sbx/tools.list? → bwrap (--share-net only if `net`) → tool run
9  sbx-run    ◄ {output, files} on stdout, or an exit code (2..8)
10 broker     valid_envelope → {output, files} only → HTTP 200
11 desk       validates output against `output.schema` → result to Pi
12 broker     (after the job) reset_sbx() → RESET_CMD → record {ok, rc, at}
```

✗ Any hop refuses rather than degrades: 403 unknown_tool, 503 lane_busy, 503 sandbox_not_reset, 502 with a `REASONS` status
and **no sandbox text**. Exit codes are the failure vocabulary all the way up.

## F5 — Firewall: three separate flows

**F5a. Switch profile** (the console's Firewall tab)

```
UI → POST /api/fw/profile {active:"build", for_s:900}
   → fw_set_profile → _current_policy + active_until
   → fw_apply(instant=True) → validate → render → nft -c → nft -f → _save(ruleset.nft, policy.json)
   → expiry: _expiry_loop every 5 s → fw_check_expiry → fw_set_profile("session")
```

`build` may carry a timer (60 s … 24 h); `session` never does. An expired window is also caught at boot (F1 step 2).

**F5b. Apply and confirm** (the two-phase apply)

```
POST /api/fw/apply
  → fw_apply: validate → render → nft -c -f -
  → snapshot: nft list table inet sbxfw   (_pending.prev)
  → nft -f -      (live now)
  → _pending = {text, policy, deadline, timer=Timer(REVERT_S, _revert)}
  → 200 {pending:true, revert_in_s}
POST /api/fw/confirm → cancel timer → _save  (the change becomes the file)
POST /api/fw/cancel  → _revert(): delete table, reload prev
timer expiry         → _revert()
```

`instant=True` is refused when the change touches `mgmt` or `sbx_ifaces` — the two fields that can cut your own access.

**F5c. Lockdown** (the gate forces `session`, never loosens)

```
gate.lockdown → POST /api/fw/lockdown (tighten token, the only route that token opens)
  → if a pending apply exists: cancel it and _revert()   (an unconfirmed change must not decide the new session)
  → if active != session: fw_set_profile("session")
  → {profile:"session", changed: bool, was}
```

The console accepts the tighten token **only** on `LOCKDOWN_PATH` (`_handle`); there is no endpoint that loosens anything.

## F6 — Events and reconnect

```
UI opens EventSource /sessions/{id}/events?since=N
  → _sse loop: events_since(N, wait=15) → write id:/data: per event → flush
  → nothing new: ": keepalive"
  → child exited and no events left: close
  → Last-Event-ID header is honoured when ?since= is absent
```

The ring is 5,000 events per session (`EVENT_RING`). ✗ A reconnect whose `since` predates the oldest retained event is
**silently truncated** — no gap event, no 409. Chat recovers by refetching `get_messages`; the inspector's timeline does not.

## F7 — Reattach after a bridge restart

```
Registry._restore → from_record(rec)
  1 cwd under current --cwd-root?        no  → refused_reason, kept, not probed, not deleted
  2 _pid_alive(pid)?                     no  → dead, record kept and listed
  3 _started_as(pid, pi_cmd)?            no  → dead  (a recycled pid is not our Pi)
  4 reopen FIFO pair                     no  → dead
  5 _probe(): get_state round trip       no  → dead
  6 live: reattach (Pi kept both FIFO ends open, so it never saw EOF)
```

`KillMode=process` in the unit is what lets the Pi children outlive a bridge restart. A dead session's channel is unlinked;
a refused one keeps its channel names so widening the root on a later start brings it back.

## F8 — Files in and out

```
POST /sessions/{id}/files?path=rel   (raw body)
  → refused_reason? → 403 · session_path() containment · Content-Length required (411)
  → > MAX_FILE (8 MiB) → 413 · write with chunk loop → 201 {path, bytes}

GET  /sessions/{id}/files?path=rel
  → containment → regular file? (404/400) → size re-checked after read → 200 octets
```

Both paths refuse `..`, absolute paths and `C:` prefixes in `session_path`, and both are under the session's `cwd`, which is
itself under `--cwd-root`.

## F9 — Peers: discover → pair → inbox → deliver

```
GET /peers
  → peers.ts_status (tailscale status --json) → online nodes with an ip
  → probe each on :9761 (GET /peer/hello) → which run a bridge → which are in the PeerStore

POST /peers/{node}/send {text}
  → no pairing yet → pair_with(): X25519 + HKDF + confirm tag (pairing.py) → PeerStore.put
  → send(): signed request (X-Bridge-Auth) → peer's /peer/message
  → peer: OwnLoginOnly.check (same tailnet, same login) → Inbox.add → 202

POST /inbox/{id}/deliver {session}
  → App.deliver: envelope() as untrusted text → lockdown() → note_user → prompt {streamingBehavior:"followUp"}
```

✗ Peer messaging is off unless the bridge runs with `--peers`; the peer listener binds the tailnet address only; the inbox
means an inbound message is data until a person delivers it.

## F10 — Blocked traffic becomes a table

```
sandbox sends something not allowed
  → forward chain: log prefix "sbxfw-drop: " (rate-limited) then drop
  → kernel journal
  → console GET /api/fw/drops → journalctl -k -g sbxfw-drop -n 500 → parse_drops()
      outbound grouped by (dir, peer, proto, dport, count), outbound first, top 100
```

✗ `journalctl` needs the service user in `systemd-journal` (the unit adds it). Without it the endpoint returns an explicit
error rather than an empty table.

## F11 — Admin: tools and settings

```
POST /api/tools/toggle|timeout|network
  → validators (_fields/_fmt) → _write_toollist(mutator) → _atomic_write /etc/sbx/tools.list
  → toollist.load() re-validates the file it just wrote (a malformed write is not left behind)
  → broker re-reads /etc/sbx/tools.list on every /run      (a change applies without a restart)

POST /api/settings
  → _v_host/_v_int/_v_user/_v_reset → _atomic_write /etc/sbx/broker.env (0640)
  → {saved, restart_required:true, hint:"systemctl restart sbx-broker"}
  → mode (lite/box) → /etc/sbxfw/console.json
```

**Drift worth knowing:** the console writes one file, `/etc/sbx/tools.list`. The sandbox box reads its own
`/opt/sbx/tools.list`, installed at build time. Disabling a tool in the UI blocks it at the broker; *enabling* a newly added
tool also needs the box's copy updated and `sync.py --deploy` run. The two can disagree, and only one of them is editable from
the console.

## F12 — Job admission: lane and reset

```
POST /run
  → busy or queued?  queue depth >= 4    → 503 lane_busy (full)
                     wait > 60 s         → 503 lane_busy (timeout)
  → _busy, run job
  → ensure_reset(): previous reset failed? → reset_sbx() once
                   still failed → 503 sandbox_not_reset {reset:{ok,rc,at}}, job never starts
  → run_on_sbx → _finish → hand the lane to the oldest waiter (_busy stays True)
  → reset_sbx() → _record_reset
  → job raised SandboxNotReset? → _handoff(), re-raise, **no job record written**
```

One job at a time, on purpose: the sandbox is a single box and the lane is held across the reset. Lite mode has no reset
(`RESET_CMD` empty) because every job already gets a fresh bwrap tmpfs.

## F13 — Image build (Windows) and the launcher

```
image/gen-keys.sh
  → build/keys/core/{broker keypair, sbx host pub}      (core never sees the sandbox's private host key)
  → build/keys/sbx/{broker pub, sbx host keypair}       (sandbox never sees the broker's private key)

enclave.cmd → enclave.ps1 → menu
  Build:  Packer stage 1  q35 + kernel-irqchip=off + preseed   → base.qcow2   (installs, powers off)
          Packer stage 2  pc, uploads repo, provision.sh      → build/packer-out/qemu/sbx-vm.qcow2 + sha256
          and the same for the lean box                       → build/packer-out/sbxnode/sbx-node.qcow2
  Start:  New-Overlay(base → run/core.qcow2) + New-Overlay(sbx-node → run/sbx.qcow2)
          Start-Vm ×2, joined by a QEMU socket network  (so the sandbox has no other path)
  Reset:  throw away run/sbx.qcow2, re-create the overlay — the clean disk the broker cannot do itself
  Verify: sha256 of the images against the recorded sums
```

## F14 — Provisioning the two boxes

```
core:  provision.sh
  packages → pins (manifest.env) → accounts (pibridge, sbxgate, sbxbroker, deskbox, sbxconsole, loom, ollama)
  → Ollama + nomic-embed-text → Loom → pi-bridge/deskbox/gate/console/stub installs
  → systemd units enabled → eth1 10.77.0.1 + ip_forward → sbx-reboot-and-wait → setup-lite.sh
                                                                  └─ sbx user, loopback forced-command key,
                                                                     known_hosts.box, broker.env, first-boot banner

box:   provision-sbx.sh   (needs internet: apt)
  packages → sbx user + forced command → /opt/sbx → reset helper + sudoers → pinned host key
  → nftables (input drop except 10.77.0.1:22) → staged net + sshd config in /usr/local/share/sbx-node
       finalize-sbx.sh  (after the cable is in, at the console)
  → static 10.77.0.2/24 gw 10.77.0.1 → sshd drop-in → nohook resolv.conf → IPv6 off → chattr +i resolv.conf
```

## F15 — Adding a tool (the pipeline the code insists on)

```
1 tools.list            add `fetch 30 net` (or without net)
2 sbx/tools/fetch/run   the program that runs on the sandbox box
3 harness/tools/fetch-sbx/tcs.yaml   contract: input schema, output schema, network:false, sandbox.bind broker sock
4 python3 sync.py       regenerate the stub, validate every contract, report orphan -sbx dirs
5 sync.py --deploy DIR  install contracts at 0700 into the desk's tools dir
6 deploy tools.list to /etc/sbx/tools.list and /opt/sbx/tools.list
```

`sync.py` refuses a contract that is not `network: false` or that does not bind the broker socket — that pair is what keeps
"all tool calls go through the desk" true. `tools.list`'s own comment forbids `net` on anything that executes handed code.

## F16 — Smoke tests, and what they actually cover

```
core:  sbx-smoke-test        (image/smoke-test.sh, ~28 checks)
       units up · listeners loopback-only · nft policy + gateway · gate · desk posture · broker socket · greet end to end
       sbx-smoke-desk       (image/smoke-test-desk.sh) desk only: enforcement block, socket, account, adapter
box:   sbx-node-test         (image/smoke-test-sbx.sh) one HTTPS request, --expect blocked|open
```

`--quick` drops the egress probe and the end-to-end greet. In box mode the check runs against `10.77.0.2`; in lite mode
against loopback.

---

## Failure map

| Symptom | Status | Raised in | Meaning |
|---|---|---|---|
| `bad Host header` | 403 | `server._guard` | a web page tried to drive the API |
| `outside_roots_error` | 403 | `_dispatch` /sessions | cwd outside `--cwd-root` |
| `firewall lockdown failed, session not started` | 503 | `gate_lockdown` | console or gate down: fail closed |
| `firewall lockdown failed, input not sent` | 503 | `_dispatch` /command | same, mid-session |
| `unknown_tool` | 403 | broker | tool not in `/etc/sbx/tools.list` |
| `sandbox_not_reset` | 503 | broker | last reset failed; no job will run until one succeeds |
| `lane_busy` | 503 | broker | queue full (4) or waited 60 s |
| `bad_envelope` | 502 | broker | sandbox returned something that is not `{output, files}` |
| `timeout` / `toobig` / `rc5` … | 502 | broker `REASONS` | exit-code vocabulary, never sandbox text |
| `content-length required` / `-` | 411/413 | `_upload` | raw body without a size, or over 8 MiB |
| `no token` at start | exit | `console.main` | run `sbx_console.py --init` once |
