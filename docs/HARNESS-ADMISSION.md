# Harness admission

Written 2026-10-18, before any second harness exists. It records what a harness must give up to run next to Pi, and the checks a
reviewer runs before believing it did. It builds on the 2026-10-18 decisions in `TODO.md` (MCP declined; a tool is a file on disk
under the desk, not a script the agent execs).

Claims name the file and the field or function to check, not a line: the tools/desk work landed on branch `desk-tools` and moved
those files, so a line number would be stale before the next review.

## 1. What the core protects, and what enforces it

The core holds the model keys, the broker's SSH key, the console's two tokens, the tool contracts and entry points, Loom's memory,
and the broker's log. The service accounts sit around those things:

| Account | Created by | What it can reach, from the code |
|---|---|---|
| `pibridge` | `image/provision.sh` (`useradd` for `pibridge`) | `127.0.0.1:9760`; its children inherit `/run/sbx/secrets.env` (`root:pibridge 0640`); is in group `sbxgate`, which is what lets it connect to `/run/sbx-gate/gate.sock` (`0660` group `sbxgate`) but cannot read `/etc/sbxfw/tighten-token` (`0640` group `sbxgate`); writes `/var/lib/pi-bridge` |
| `sbxgate` | `image/provision.sh` | `/run/sbx-gate/gate.sock`; reads `/etc/sbxfw/tighten-token` and may call `/api/fw/lockdown` and nothing else. Not in `pibridge`, so it cannot read `/run/sbx/secrets.env` |
| `sbxbroker` | `image/provision.sh`, `image/setup-lite.sh` | the broker listener; `/etc/sbx/id_ed25519` (`0600`); SSH to the sandbox forced command; `RuntimeDirectory=sbx` in its unit |
| `deskbox` | `image/provision.sh`, `harness/systemd/deskbox.service` | `127.0.0.1:8080` (`-strict`); `/opt/deskbox/tools` at `0700`; `/var/lib/deskbox`; group `sbxdesk`, which is the group on the broker socket |
| `sbxconsole` | `image/provision.sh` | `127.0.0.1:8787`; `CAP_NET_ADMIN`; reads and writes `/etc/sbxfw` and `/etc/sbx` |
| `loom` | `image/provision.sh` | `127.0.0.1:9741`; calls `127.0.0.1:11434`; writes `/var/lib/loom` |
| `ollama` | `image/provision.sh` | `127.0.0.1:11434`; writes `/var/lib/ollama` |

In lite mode there is a further account, `sbx`, with `/bin/sh` (`image/setup-lite.sh`); the broker's key is locked to the forced
command, and a harness must not run as it either. Outside this set, `image/provision.sh` installs `tailscaled` as root with
`CAP_NET_ADMIN` and `CAP_NET_RAW`; root is outside every file mode below.

The enforcement is not the desk. In order of strength:

- The VM split. The sandbox reaches nothing except through the core's forward chain, which `console/sbx_console.py` builds with
  input and forward `policy drop` and only allowlist accepts, while the core's own output is `policy accept` (the ruleset
  template in that file). `image/smoke-test.sh` checks it in `check_nft_policy` and `check_gateway`.
- `sbx-run` plus bwrap inside the sandbox: `--unshare-all`, plus `--share-net` only for a tool whose `tools.list` entry says
  `net`; cleared environment, tmpfs `/tmp`, read-only `/usr`, per-job rlimits (`sbx/sbx-run`). It is reached only through the
  forced command installed by `image/setup-lite.sh`.
- The broker's allowlist and envelope: `tools.list` re-read per request, 1 MiB in and out, only `{"output", "files"}` accepted,
  failures reported as exit codes and never as sandbox text (`harness/broker/sbx_broker.py`).
- Unix accounts and file modes, as in the table above.

The desk is a contract, audit and durability layer. A `tcs.yaml` declares input and output schemas, a timeout, allowed side
effects, and any host paths to bind; `sync.py` refuses to deploy a contract that is not `network: false` or that does not bind
the broker's socket, and `sync.py --deploy` installs the stub at `0700`. DeskBox queues the job and runs it in its own bwrap; the
stub says the desk validates the contract after the call, and the console only hashes files for display (`tool_status`). Whether
the contract is enforced is DeskBox's behaviour at `DESKBOX_COMMIT`, not something this repo shows.

It is not a jail:

- DeskBox's `network: true` is all-or-nothing host egress: a contract that declares it gives its job the core's internet, which
  the console never sees. No shipped contract uses it — the `*-sbx` stubs are `network: false` and reach the broker over a
  bound unix socket instead (`harness/tools/greet-sbx/tcs.yaml`, `harness/stub/run.py`) — but a new tool can still ask for it.
- bwrap bounds a job's filesystem and network namespace, not a kernel or hypervisor escape; the core's loopback services are
  still reachable from any process that can open them.
- The desk is in the path only while its unit runs with `-strict`. `check_desk_service` in `image/smoke-test.sh` fails unless
  `GET /` reports `enforcement.strict`, `sandbox`, `seccomp` and `resource_limits`, and not root. The unit belongs in
  `harness/systemd/`, and `image/provision.sh` installs it.

What a process in the core can reach, whatever its account:

- The broker listener: no authentication. In this tree it is a unix socket (`SBX_SOCKET`, `/run/sbx/broker.sock`) created
  `0660` group `sbxdesk`, and TCP only if `SBX_LISTEN` is set (`harness/broker/sbx_broker.py`). Whatever can open it runs any
  tool in `tools.list` without a contract, so `sbxdesk` membership is equivalent to tool access; `check_greet_e2e` in
  `image/smoke-test.sh` calls it that way over the socket.
- `127.0.0.1:8080` (desk): `-strict`, but `check_desk_e2e` calls it with no credential, and whether DeskBox authenticates its
  callers is not verified here. Any local process that can reach the port can ask the desk to run a declared tool.
- `127.0.0.1:9760` (pi-bridge): no login; a loopback `Host` and same-origin `Origin` only (`harness/bridge/server.py`, `_guard`),
  which stop browsers, not local processes.
- `127.0.0.1:8787` (console): bearer token. `token` is `0640` group `sbxconsole`; `tighten-token` is `0640` group `sbxgate` and
  is accepted only on `/api/fw/lockdown` (`console/sbx_console.py`). A `pibridge` process cannot read it: the bridge reaches the
  lockdown through `sbx-session-gate`'s socket instead (`harness/gate/session_gate.py`).
- `/run/sbx-gate/gate.sock` (session gate): `0660` group `sbxgate`, so the bridge's account may connect and nothing else in the
  core may. The gate exposes `lockdown` and no verb that loosens.
- `127.0.0.1:9741` (Loom): no authentication on any route; the loopback bind is the only access control (`image/provision.sh`,
  noting it of Loom's source, not verified against `LOOM_COMMIT`). `11434` (Ollama) has no token configured either.
- Files: `/etc/sbx/id_ed25519` `0600 sbxbroker` (`image/setup-lite.sh`); `/run/sbx/secrets.env` `0640 root:pibridge`
  (`image/sbx-fetch-secrets.sh`); `/etc/sbx/bws-token` `0600 root` (`image/provision.sh`).

A harness's isolation from all of that is its Unix account and those file modes, nothing else. The session gate forces the
sandbox's firewall profile before a session starts (the `POST /sessions` path in `harness/bridge/server.py`), but the core's
output chain is `policy accept`, so the gate says nothing about what the harness itself may reach. `check_outbound` in
`image/smoke-test.sh` records it as expected: "core can reach the internet (open by design)".

## 2. The admission test

Every item has to pass before a harness is admitted, and the evidence is recorded in that harness's admission note with the date,
image hash and harness version. Run the checks as the candidate's own account on a rebuilt image.

1. Every built-in exec, file and network primitive is disabled or rerouted.
   Must: have no tool that spawns a process, reads or writes an arbitrary path, or makes an arbitrary request, unless that tool's
   implementation calls the desk tool of item 2. Asking the model for a shell or a file write produces no shell and no file.
   Settled by: the candidate's tool definitions as the model sees them, pinned by path and hash, with no shell, read, write,
   edit or fetch tool; a session that asks for `id` and a write to `/tmp/x`, after which `ps -eo user,pid,ppid,args` and the
   unit's cgroup (`/sys/fs/cgroup/system.slice/<unit>/cgroup.procs`) show no shell child and `/tmp/x` does not exist.
   Not verified for Pi: nothing in this repo narrows Pi's tools, and the unit passes no `--pi-arg`
   (`harness/systemd/pi-bridge.service`, `harness/bridge/server.py`); the file that would have to change is that unit, or a Pi
   config installed by `image/provision.sh`.

2. Its only tool surface is DeskBox, reached through the tools adapter.
   Must: emit tool definitions generated by `harness/toolsadapter/desk_tools.py` (it reads `GET /tools`, then each
   `GET /tools/{name}` for the schemas) and route every call through desk -> stub -> broker -> sandbox. It must not exec
   `harness/tools/<name>-sbx/run.py` or the sandbox-side `run` itself (the "Where tools live" decision in `TODO.md`, 2026-10-18).
   Settled by: the adapter source and its tests; `desk_tools.py --desk http://127.0.0.1:8080 openapi` producing the tool
   definitions the harness actually loads; the deployed tool directory at `0700` from `sync.py --deploy`; the contract passing
   `sync.py`'s `network: false` and broker-socket checks; one broker log line per call (`harness/broker/sbx_broker.py`).
   Not yet run against a real desk: the adapter's wire format comes from DeskBox's handlers, not from an observed session.

3. It runs as its own Unix account.
   Must: a system user with `nologin`, named by `User=` in its unit, not `pibridge`, `sbxbroker`, `sbxconsole`, `deskbox` or
   `sbx`, and not in their supplementary groups (in particular not `sbxdesk`), with no read of `/run/sbx/secrets.env`,
   `/etc/sbxfw/token`, `/etc/sbxfw/tighten-token`, `/etc/sbx/id_ed25519`, `/etc/sbx/bws-token`, `/var/lib/pi-bridge` or
   `/opt/deskbox/tools`.
   Settled by: `id <user>`, `systemctl show -p User,SupplementaryGroups <unit>`, and the read probe per path as the account
   (`runuser -u <user> -- test -r <path>`, the pattern `check_secrets` in `image/smoke-test.sh` uses for `pibridge`). Probe the
   console's `tighten-token` too: if the account can read it, item 4 is being paid for by a second copy of the gate.

4. It starts a session only after the gate has forced the `session` profile, and fails closed.
   Must: refuse to start a harness process when the gate is unreachable or answers badly; never fall back to "start anyway".
   Settled by: the unit passing `--gate-socket` (`harness/systemd/pi-bridge.service`), and a run with the gate stopped where
   `POST /sessions` answers 503 and no harness process exists. The tests are the shape to copy: `FirewallLockdown` in
   `harness/bridge/tests/test_bridge.py` (`test_unreachable_gate_is_a_refusal`). Note the code's default: without
   `--gate-socket` a session starts with no gate at all (`test_not_configured_means_no_call`, same file), so a unit that omits it
   fails this item. Any second bridge calls the same `sbx-session-gate`, never a copy of the lockdown.

5. Model keys arrive through the secrets env and never from a file.
   Must: take keys from the process environment, written by a root `ExecStartPre` before the service user exists
   (`image/sbx-fetch-secrets.sh`), or from a per-account file with the same ownership rules. The image, the repo and the harness
   config must contain no key, and the harness account must not read `/etc/sbx/bws-token` or `/etc/sbx/bws.env`.
   Settled by: `EnvironmentFile=` and `ExecStartPre=+` in the unit (`harness/systemd/pi-bridge.service`); a search of the
   installed image for key-shaped strings returning nothing; the harness config naming environment variables, not values;
   `runuser -u <user> -- test -r /etc/sbx/bws-token` failing.

6. It runs without privilege and listens on loopback only.
   Must: `NoNewPrivileges=yes`, `ProtectSystem=strict`, `CapabilityBoundingSet=` like the existing service units, no setuid
   helper, and no listener outside `127.0.0.0/8` or `::1`.
   Settled by: the unit file; `ss -ltnH`, where any non-loopback address is a failure (`check_listeners` in
   `image/smoke-test.sh` makes exactly this check); `grep CapEff /proc/<pid>/status` for the running process.

7. It accepts one sandbox lane.
   Must: tolerate its tool calls queuing behind another harness's, with a timeout that accounts for the queue.
   Settled by: one call from each harness at the same time, both completing, the second's wall time covering the first's.
   The numbers do not fit today: the broker caps a job at `MAX_TIMEOUT` (120 s) plus 15 s of grace
   (`harness/broker/sbx_broker.py`), while `harness/tools/greet-sbx/tcs.yaml` declares `execution.timeout_ms: 150000` and the
   stub waits 150 s (`harness/stub/run.py`, `UnixConnection(timeout=150)`). A queued job can outlive the stub that waits for it.

Not verified from this repo, and so not settled by reading it: Pi's built-in tool set and whether its exec or file tools are
reachable (no config here; the file that would have to change is `harness/systemd/pi-bridge.service` or a config installed by
`image/provision.sh`); DeskBox's own behaviour, including `GET /tools`, contract validation, `-strict` and seccomp (external
repo at `DESKBOX_COMMIT`); Loom's lack of authentication (read from a comment in `image/provision.sh`, not from Loom's source);
whether deployed tools match the repo (`sync.py` checks the repo, deployment is the `--deploy` step plus `image/SETUP.md`).

## 3. What disqualifies a harness outright

- It needs a shell it cannot live without. A persistent shell, or a single "terminal" tool as the whole tool surface, cannot be
  routed through the desk: by design the desk runs one declared tool per job in its own bwrap, not a stateful shell. Admitting it
  means giving the core a shell, which gives up items 1, 2 and 3 at once.
- It needs general network egress from the core. An agent-facing fetch or curl tool running in the harness process, a listener
  that is not loopback, or a proxy the agent controls, is unfiltered core egress: the console's rules are the forward chain for
  the sandbox interface, and the core's output is `policy accept`. A tool with `net` in `tools.list` is not this; it runs in the
  The forward chain filters what the sandbox *forwards*, which is the full-mode path. In lite mode the sandbox is this VM, so a net tool shares the core's namespace and reaches loopback (desk, bridge, console, Loom) without the forward chain ever seeing it.
  not this either; an arbitrary request primitive is.
- It installs its own tool protocol server in the core. MCP is declined (`TODO.md`, 2026-10-18) because it turns a pinned tool
  surface into prompt text served at runtime by a server inside the key-holding VM. A harness that only speaks MCP adds that
  surface next to the desk and does not move the enforcement floor, which is below the protocol.
- It requires root or a capability. Root ends the account boundary and reads `/etc/sbx/id_ed25519`, both console tokens, Loom and
  the secrets file with no file-mode check. `sbxconsole`'s `CAP_NET_ADMIN` is the existing example of a service account that can
  change the firewall; a harness has no equivalent need.

## 4. What admitting one anyway costs

- The account boundary is the load-bearing part, and it moves the decision rather than removing it. A harness account that is
  not in `sbxdesk` cannot open the broker socket, so it needs the desk (item 2); an account added to `sbxdesk` gets every tool
  in `tools.list` with no contract, which is the access the desk exists to bound. Either way the new uid still reaches the
  desk's port, the bridge, Loom and Ollama.
- Secrets stop being one file for one account. `/run/sbx/secrets.env` is `root:pibridge 0640`. A second account needs its own
  root-written file (a change in `image/sbx-fetch-secrets.sh`) or membership of the group that reads all keys; the second option
  means a compromise in either harness reads the other's keys.
- The gate is shared, so the second harness does not get its own. Both call `sbx-session-gate`; the failure mode moves to the
  gate's availability, because a bridge that cannot reach it refuses to start a session.
- One sandbox, one lane. `SerialLane` in `harness/broker/sbx_broker.py` runs one job at a time, holds the lane through
  `SBX_RESET_CMD`, and answers `503 lane_busy` once `SBX_QUEUE_MAX` callers are waiting or `SBX_QUEUE_WAIT_S` expires. A second
  harness competes for that lane with the first; `GET /status` on the same socket is how you see who is waiting and what the
  last reset did.
- Audit loses attribution. The broker's log line records the tool, its `net` flag, status, size, hash and duration, not the
  caller (`harness/broker/sbx_broker.py`). With two harnesses a tool call cannot be attributed to one of them from that log;
  changing that means changing the broker.
- Pi sets the bar and does not clear it. Items 1 and 2 are unverified for Pi as well. Admitting a candidate that clears them makes
  `pi-bridge` the outlier and puts it on the same list; admitting one that does not keeps the standard at "whatever Pi does",
  which is the outcome this document exists to avoid.

## 5. What this document is not

It is not a certification. A harness that passes section 2 is admitted to run next to Pi, not trusted: the enforcement stays the
VM split, `sbx-run`/bwrap, the broker allowlist and Unix file modes, none of which this document strengthens.

It does not replace the independent review (TODO task 11). The checklist is a reviewer's script, not a second opinion: the gateway
and UI design still need review by someone who is not the author of this repo. It is also not a review of DeskBox, Pi or Loom;
their guarantees belong to their pinned commits (`image/manifest.env`).
