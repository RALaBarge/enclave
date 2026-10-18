# Tools/desk track: review report

2026-10-18. Branch `enclave:desk-tools` (32 commits on top of `main`), DeskBox branch `deskbox:sandbox-bind`
(`4a39e508dccccf39ecd54f0db4b7c9bc356026d2`, pinned as `DESKBOX_COMMIT` in `image/manifest.env`).

This is the written record of what was built, what was actually run, what the reviews found, and what is still open.
Every claim below is either a command with its output or is marked as unverified.

## 1. Scope

Twelve items from `TODO.md`: 4, 6, 7, 8, 9, 10, 15, 16, 17, 18, 19, 20. The rest of the file was left alone; tasks 1,
2, 3, 5, 11, 12, 13, 14 need a VM, a human, or the host, and are untouched.

The intent, stated by the owner: every tool call goes through DeskBox; the desk runs as its own account; the tool stubs
reach the broker over a socket instead of being given the host network; and a second harness should be able to use the
same tools without its own tool code or its own copy of the session-lockdown guarantee.

## 2. What was built

| # | Item | Where |
|---|---|---|
| 15 | DeskBox as a service, `deskbox` account, `sync.py --deploy`, smoke checks | `harness/systemd/deskbox.service`, `image/deskbox-run.sh`, `image/provision.sh`, `sync.py`, `image/smoke-test.sh` |
| 16 | Broker on a unix socket; `sandbox.bind` in DeskBox; stubs `network: false` | `harness/broker/sbx_broker.py`, `harness/systemd/sbx-broker.service`, `harness/stub/run.py`, `harness/tools/*-sbx/tcs.yaml`, deskbox `internal/tcs/bind.go` |
| 17 | Tool-definition adapter generated from the desk | `harness/toolsadapter/desk_tools.py` |
| 18 | Session gate: one process owns the lockdown and the tighten token | `harness/gate/session_gate.py`, `harness/systemd/sbx-session-gate.service`, `harness/bridge/server.py` |
| 19 | Bounded, observable serial lane; failed reset gates the next job | `harness/broker/sbx_broker.py` |
| 20 | Admission criterion for a second harness | `docs/HARNESS-ADMISSION.md` |
| 7 | Tailscale, pinned, no key in the image, tailnet peer rule | `image/manifest.env`, `image/provision.sh`, `console/sbx_console.py` |
| 8 | Per-tool `net` flag | `common/toollist.py`, `sbx/sbx-run`, `console/sbx_console.py`, `tools.list` |
| 9 | Reset hooks | `image/sbx-reboot-and-wait.sh`, `image/sbx-reset.sh`, `sbx/sbx-channel` |
| 10 | Session persistence across restarts; uploads/downloads | `harness/bridge/pibridge.py`, `harness/bridge/server.py` |
| 4 | Per-turn logging to Loom | `harness/bridge/server.py` |
| 6 | `rsync` tool | `sbx/tools/rsync/`, `harness/tools/rsync-sbx/` |

## 3. Verification: what was run

Everything below was run on this machine on 2026-10-18. The Linux side is WSL2 (Debian, kernel 6.18) with Go 1.27 and
Python 3.13, `bwrap` present, user namespaces available.

### 3.1 DeskBox, including the new `sandbox.bind`

```
GOOS=linux go vet ./...     -> clean
GOOS=linux go build ./...   -> ok
go test ./...               -> cmd/agent-desk FAIL (TestSeccompDeniesPtrace)
                               internal/tcs ok
```

`TestSeccompDeniesPtrace` fails identically on unmodified `HEAD` in this WSL (`ptrace(TRACEME)` is refused by the host),
so it is the environment, not the branch. Everything else in that package passes, including the six new tests.

End to end, against a desk built from the pin:

- a tool with `sandbox.bind` connected to a host unix socket from inside its job and returned `{"reply": "pong:ping"}`,
  with `network: false` in force and the enforcement block reporting `sandbox: True, seccomp: True, resource_limits: True`;
- the same tool with the bind removed failed to connect — the bind is what enables it, not ambient access;
- a bind with `dst: /etc/passwd` was refused at load: `refusing to load tool "badbind": sandbox.bind[0]: dst
  "/etc/passwd" must live under /deskbox`.

### 3.2 The stub and the broker, over the real socket

Real `harness/stub/run.py` against the real `harness/broker/sbx_broker.py`, with `ssh` faked so no sandbox box is needed:

```
socket:  srw-rw----  (0660)
/status: {"busy": false, "queued": 0, "depth_max": 4, "lane": "serial", "reset": {"ok": true, "rc": 0, "at": null}}
stub:    {"greeting": "Hello, fake.", "length": 11}      rc=0
TCP 8099: closed
```

The socket group fell back to the broker's own group on that host because `sbxdesk` does not exist there, which
exercised the missing-group path rather than the intended one.

### 3.3 The adapter, against a desk rather than a fixture

`harness/toolsadapter/desk_tools.py` was pointed at that same desk. `fetch` read the real `GET /tools` and, per tool,
`GET /tools/{name}`; `call` went desk -> bwrap job -> bound socket -> host listener and returned `{"reply": "pong:ping"}`.
This retires the adapter's biggest caveat: its wire format is now observed, not inferred.

It also corrected a claim I had made earlier: `GET /tools` lists tools but does **not** carry the schemas. An adapter
written to that claim would have emitted an empty input schema for every tool and accepted anything.

### 3.4 Suites

| Suite | Result |
|---|---|
| `harness/gate` | 6 tests, OK |
| `harness/bridge/tests` | 39 tests, OK |
| `harness/toolsadapter` | 25 tests, OK |
| `harness/broker` | 11 tests, OK |
| `harness/tools/rsync-sbx` | 52 tests, OK |
| `console/test_console.py` | 32 tests, OK |
| `sync.py --check` | 2 tools, rc 0 |
| `tcs-verify` on both contracts | all 2 contracts OK |
| `bash -n` on every shell file touched | clean |
| `systemd-analyze verify` on the four units | parses; only "marked executable" and "binary not installed here" |

Three negative controls, because a check that cannot fail is not a check:

- `sync.py --check` from `/tmp` (previously crashed on a relative path) now passes: rc 0;
- a contract with two `network:` keys is refused (`found ['true', 'false']`);
- a contract with `default: false` (a keyword DeskBox silently ignores) is refused by `tcs-verify`, which `sync.py`
  now runs when it is on `PATH`;
- a stub edited by hand is reported as `stub out of date`.

## 4. What the reviews found

Two independent reviews ran against the branch. Both reported after being asked to stop, and both found things. Fixed on
the branch:

1. **The desk could never start.** In a system unit `%U` is the manager's UID (0), not `User=`, so the unit waited on
   `/run/user/0/bus` and restart-looped; `ProtectHome=yes` also hid `/run/user`. The environment and the wait moved
   into `image/deskbox-run.sh`, which asks `id -u deskbox`, and `ProtectHome` is gone.
2. **The bridge file API was a confused deputy.** `POST /sessions {"cwd": "/run/sbx"}` followed by
   `GET /sessions/<id>/files?path=secrets.env` read the model keys as the bridge's account; `/etc/sbxfw` did the same for
   the console tokens. The resolved cwd and the resolved target are both checked against the secret directories now. The
   first version of this fix checked only the cwd, which the reviewer defeated with `cwd: /run` + `path: sbx/secrets.env`;
   that is what the second check is for.
3. **`sync.py` crashed outside the repo root** — it resolved the contract path against the caller's directory.
4. **A contract keyword DeskBox would have ignored** — `rsync-sbx` declared `default: false`, which is outside the
   schema subset, so the desk accepted the contract while the key meant nothing. `sync.py` now calls `tcs-verify`.
5. **A second broker could unlink a live socket** (the first would keep serving an unlinked inode). It now probes with a
   connect and refuses if something answers; a stale file is still reclaimed and a non-socket file is still never
   deleted.
6. **The gate ran with the bridge's group**, so the process that forces a firewall profile could read the secrets env.
   Split into `sbxgate` (socket, token) and the bridge's membership of it.
7. Smaller: the adapter was not installed into the image; the sandbox image had no `rsync`/`openssh-client`; a `net` tool
   had no `/etc/resolv.conf` or `/etc/passwd` inside its job; the Loom redactor was not installed beside the bridge.

## 5. What is still open

`TODO.md` carries the full table (A–P), each with the file to start from. The ones that are defects rather than polish:

- **A.** The stub's 150 s client versus a broker worst case of queue wait (60 s) + tool wall (timeout + 15) + reset
  (300 s) = 195–495 s. Under two harnesses the stub gives up on a job that is still running.
- **B.** `rsync_tool.py` passes no `timeout=` to `subprocess.run`, so its `TimeoutExpired` branch is dead code.
- **C.** The rsync allowlist bounds the host, not the remote path: `name:~/.ssh/authorized_keys` is accepted with
  `apply: true`.
- **G.** DeskBox `sandbox.bind` accepts `dst: /deskbox/tool` and binds are emitted after the fixed mounts, so a contract
  can shadow the script the integrity pin hashed.
- **H.** After the group split the token is `sbxconsole:sbxgate 0640` and the bridge is in `sbxgate`, so the bridge can
  still read it. The socket needs its own group.
- **I.** The gate smoke check asserts `active`, the console returns `profile`; the gate's unit test uses a fake that
  returns `active`, so the test passes and the smoke check would fail.
- **L.** `sync.py --deploy` follows a symlink in a directory owned by the tool-running account while deploy is documented
  as root.
- **M.** A malformed POST (`Content-Length: abc`, or a JSON body that is a list) drops the connection instead of
  answering 400.

## 6. Limits of this report

- **No image build and no VM run.** Nothing here has been through Packer, QEMU, systemd on a real boot, nftables, or
  `sbx-smoke-test`. Every service unit, every provisioning step and the whole smoke test are unexecuted.
- **The integration proofs were ad hoc scripts**, run from a temp directory and not committed. They are reproducible from
  the commands in §3, but a reviewer cannot re-run them from the branch.
- **The DeskBox pin points at a local commit.** `4a39e50` exists only in this clone; it must be pushed before anyone
  building from a clean checkout can resolve it.
- Two reviews found two blocking defects in a branch that had passed every suite it had. That is the strongest argument
  for the VM run before anything here is trusted, and it is why the smoke test's own gaps (I, J) are on the list rather
  than assumed away.
