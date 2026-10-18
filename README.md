# Enclave

A place to run an AI agent where you decide what it can reach. The agent's harness (Pi), its memory (Loom), your tools and your
keys live in a **trusted core**. Whatever the agent *executes* runs in a separate **sandbox** that knows only the core, which acts as
its gateway: the sandbox's traffic goes out only as far as an allowlist says, and a block list always wins.

```
                       +-----------------------------+
   you / UI  --------> |  core (trusted)             |           +---------------------------+
   (loopback,          |  Pi harness, Loom, Ollama,  |  eth1     |  sandbox                  |
    SSH tunnel)        |  DeskBox, pi-bridge, broker | <-------> |  sbx-run -> bwrap per tool|
                       |  console + firewall         | 10.77.0.x |  holds no keys or secrets |
                       +--------------+--------------+           +---------------------------+
                                      | eth0
                                  internet   <- only what the allowlist (session or build profile) lets through
```

## What is in here

| Path | What |
|---|---|
| `harness/bridge/` | **pi-bridge**: HTTP/SSE API over Pi's RPC mode, one Pi process per session; optional tailnet peer messaging. Calls the session gate before any session starts. |
| `console/` | **console**: the firewall (profiles, allow and block lists, instant apply, build timer), the tools list, settings; a small web page. `test_netns.sh` proves the rules on a real kernel. |
| `harness/gate/` | **session gate**: the one place a new session's firewall profile is forced. Holds the tighten-only token, exposes `lockdown` and nothing that loosens, on a unix socket owned by the bridge's group. |
| `harness/broker/` | **broker**: sends tool calls from the core to the sandbox over SSH (forced command), on a unix socket only the desk's group may connect to. |
| `deskbox` (pinned in `image/manifest.env`) | the **desk** every tool call goes through: contract check in, contract check out, each run in its own bwrap. Runs as its own account; the tools it serves live in `/opt/deskbox/tools`, deployed by `sync.py --deploy`. |
| `sbx/` | the sandbox side: `sbx-run` and the tools, each in its own `bwrap` with no network unless its `tools.list` line says `net`. |
| `image/` | builds the VM images with Packer + QEMU: the **core** (`sbx-vm`) and the lean **sandbox** (`sbx-node`, no secrets), plus `enclave.ps1` (repo root), the launcher. |
| `docs/HARNESS-ADMISSION.md` | what a second harness must give up before it may run next to Pi: what the core actually enforces, the admission checklist, the disqualifiers, and the cost. |

Code identifiers keep the older `sbx` name (`sbx-console`, `/opt/sbx`, the `sbxfw` firewall table); Enclave is the project.

## Try it

```powershell
enclave.cmd                                  # menu: build images, start, stop, reset
```

The launcher builds the images (Packer + QEMU, see `image/packer/README.md`) and boots the core and sandbox VMs. The core keeps a
persistent disk, so its password, keys and data survive restarts; the sandbox starts fresh each time. `enclave.cmd status|start|stop`
and `enclave.cmd build sbx|core|all` do one thing without the menu.

Then follow `image/SETUP.md`. On the core: `sudo sbx-mode box`, `sudo sbx-smoke-test`. On the sandbox: `sudo sbx-node-test`.

## Tests

```
python console/test_console.py                           # 32 tests
python -B -m unittest discover -s harness/bridge/tests    # 24 tests
python -B -m unittest discover -s harness/gate            # 6 tests, Linux (AF_UNIX + grp)
bash console/test_netns.sh                               # 28 checks, real nftables, throwaway namespaces (WSL/Linux, root)
```

The broker's queue and the tools adapter have their own `test_*.py` next to them. Nothing in this list needs a VM; the
bwrap, nftables and systemd paths they describe do.

## Honest limits

A VM is not a hardware boundary: the strongest setup runs the sandbox on its own device. Lite mode (one VM) shares a kernel between
core and sandbox. Nothing here protects data you hand the agent. See `image/SETUP.md` and `TODO.md` for status and open work.

Licence: MIT (`LICENSE`). The bundled DeskBox binaries carry the licence of the `ralabarge/deskbox` commit they were built from.
