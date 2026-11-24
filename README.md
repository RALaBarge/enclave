# Enclave

A place to run an AI agent where you decide what it can reach. The agent's harness (Pi), its memory (Loom), the tool desk
(DeskBox) and every key live on a **trusted core**. Whatever the agent *executes* runs on a separate **sandbox** that knows only
the core. The core is the sandbox's gateway: the sandbox's traffic goes out only as far as an allowlist says, and a block list
always wins.

```
                           +-----------------------------------+
   you                     |  core (trusted, holds the keys)   |              +-----------------------------+
   ssh over Tailscale ---> |  Pi + pi-bridge + web UI          |   eth1       |  sandbox (holds nothing)    |
   tunnel to the UI and    |  DeskBox, broker, session gate    | <----------> |  sshd, forced command only  |
   the console             |  Loom, Ollama, console + firewall |  10.77.0.1   |  sbx-run -> bwrap per tool  |
                           +-----------------+-----------------+  10.77.0.2   +-----------------------------+
                                             | eth0
                                  LAN / internet  <- the core's own traffic (model APIs, Bitwarden);
                                                     the sandbox's only as far as the active profile allows
```

## How it runs today

The current deployment is two small x86 boxes (Dell Wyse thin clients) joined by a private Ethernet cable: the core is
installed on bare metal with `image/provision.sh`, the sandbox with `image/provision-sbx.sh`, and the sandbox has no
other network. The same code also builds and runs as two QEMU VMs on a Windows PC (`enclave.cmd`), which is how it was first
built and verified; that path still works but is not what runs day to day.

A tool call goes: Pi -> the desk extension -> **DeskBox** checks the input against the tool's contract -> the tool's stub
-> the **broker** (unix socket) -> SSH with a forced command -> `sbx-run` on the sandbox, which runs the tool in its own
`bwrap` -> the result goes back the same way and DeskBox checks it against the contract before Pi sees it.

## What is in here

| Path | What |
|---|---|
| `harness/bridge/` | **pi-bridge**: HTTP/SSE API over Pi's RPC mode, one Pi process per session, loopback only (`127.0.0.1:9760`). Serves the web UI, calls the session gate before any session starts, optional tailnet peer messaging (off by default). Also `desk-extension.js`, Pi's only tools: the desk's. |
| `harness/ui/` | the **web UI** served by pi-bridge at `/ui/`: sessions, chat (text, thinking, and each tool call with its request and result), directories, tree, inspector. No build step; each module runs in its own Worker. |
| `console/` | the **console** (`127.0.0.1:8787`, bearer token): firewall (session and build profiles, allow and block lists, DNS, management access, blocked attempts), tools (enable, timeout, the code and contract behind each), activity (the desk's recent jobs with request and result), settings (read-only). `test_netns.sh` proves the rules on a real kernel. |
| `harness/gate/` | **session gate**: the one place a new session's firewall profile is forced. Holds the tighten-only token and exposes nothing that loosens. |
| `harness/broker/` | **broker**: one job lane to the sandbox over SSH (forced command), on a unix socket only the desk's group may use. Locks the firewall to session before any `net` job and can reboot the sandbox between jobs. |
| `harness/stub/`, `harness/toolsadapter/` | the stub every `-sbx` tool runs under DeskBox (talks to the broker, nothing else), and the adapter that turns desk contracts into a harness's tool definitions, including how much of a result goes inline. |
| `harness/redact/` | redaction of each settled turn before pi-bridge logs it to Loom. |
| `deskbox` (pinned in `image/manifest.env`) | the **desk** every tool call goes through: contract check in, contract check out, each run in its own bwrap, as its own account. Binaries in `image/artifacts`. |
| `tools/` | the sandbox tools: the [DeskboxTools](https://github.com/RALaBarge/DeskboxTools) submodule (`tools.list`, and per tool its `run` and contract). Clone with `--recurse-submodules`. |
| `desk-tools/` | tools that run on the core under the desk instead of on the sandbox, because they need a secret the sandbox must never hold: `fanout` (ask several models, one synthesises). |
| `sbx/` | the sandbox side: `sbx-run` (one `bwrap` per tool, no network unless `tools.list` says `net`) and `sbx-channel` (the forced command: run, reset, install). |
| `image/` | install and build: `provision.sh` (core), `provision-sbx.sh` + `finalize-sbx.sh` (sandbox), `build-pi.sh` (Pi from pinned source, run under Node), `sbx-mode`, `sbx-push-tools`, the bootstrap for a sandbox with no screen, the smoke tests, and `packer/` for the VM images. `SETUP.md` is the operator guide. |
| `sync.py` | checks every tool's contract and stub, and deploys them into the desk (`--check`, `--deploy`). |
| `docs/` | design notes: `HARNESS-ADMISSION.md` (what a second harness must give up to run beside Pi), `HARNESS-HARDENING-PLAN.md`, `PI-FEATURES.md`, `INLINE-SPILL.md`, and `atlas/`, a browsable map of every file, function and call (rebuild with `python3 atlas/build.py`). |

Code identifiers keep the older `sbx` name (`sbx-console`, `/opt/sbx`, the `sbxfw` firewall table); Enclave is the project.

## Tools

Sandbox tools (`tools/tools.list`): `greet` and `netcheck` (checks), `bash`, `read`, `write`, `edit`, `glob`, `grep` (work in
the sandbox's persistent workspace `/var/lib/sbx-work`), and `page` / `fetch`. Only `netcheck`, `page` and `fetch` have network
access. `page` and `fetch` go to one place: a gate on another machine (`browserbox`) that opens a URL in a signed-in browser tab
or fetches it, for hosts on that gate's own allowlist only. The gate address and token come from Bitwarden and are written only into
those two tools' directories.

Adding a tool means writing its `run` and contract in DeskboxTools, then `sync.py --check`, `sync.py --deploy` and
`sbx-push-tools` on the core. Nothing is installable from the UI.

## Using it

Everything on the core listens on loopback. The way in is SSH as `sbxadmin`, which Tailscale forwards to the core's loopback
sshd (`tailscale serve --tcp 22`); sshd lets that account forward to exactly two ports:

```sh
ssh -N -L 9760:127.0.0.1:9760 -L 8787:127.0.0.1:8787 sbxadmin@<core>
# http://localhost:9760/ui/   the agent: sessions and chat
# http://localhost:8787/      the console (token: sudo cat /etc/sbxfw/token on the core)
```

Secrets come from Bitwarden Secrets Manager at service start: the model keys go to pi-bridge's environment through a file in
`/run` (tmpfs), and the gate token only into the `page` and `fetch` tool directories (root:sbx, 0640) that are pushed to the
sandbox. The core keeps only the read-only Bitwarden access token on disk (root, 0600). See `image/SETUP.md`, "Secrets".

## Install

- **Two machines (what runs now):** on a fresh Debian 13 core, `sudo bash image/provision.sh`; on the sandbox box,
  `provision-sbx.sh` then `finalize-sbx.sh`, or the bootstrap the core serves to it over the cable (`sbx-bootstrap-serve`).
  Then on the core `sudo sbx-mode box`. Details: `image/SETUP.md`, "Full mode".
- **Two VMs on Windows:** `enclave.cmd` (menu: build images, start, stop, reset). Packer + QEMU with WHPX; see
  `image/packer/README.md`. Builds need a Windows checkout, not WSL (no `/dev/kvm` there).
- **Lite mode:** one machine, the sandbox is `bwrap` on the core itself (`sbx-mode lite`). Convenient, not locked down.

Check it: `sudo sbx-smoke-test` on the core, `sudo sbx-node-test` on the sandbox (every outbound attempt blocked is the pass).

## Tests

```
python3 console/test_console.py                              # console and firewall rendering
python3 -B -m unittest discover -s harness/bridge/tests       # pi-bridge (against a fake Pi)
node --test harness/bridge/tests/                             # Pi's desk extension against a fake desk
node --test harness/ui/test/                                  # the web UI modules
python3 -B -m unittest discover -s harness/gate               # Linux (AF_UNIX + grp)
python3 -m unittest discover -s harness/broker -p 'test_*.py' # Linux for the socket cases
python3 -B -m unittest discover -s harness/toolsadapter
python3 -B -m unittest discover -s harness/redact
python3 -B sbx/test_tools.py                                  # the workspace tools in sbx-run's bwrap
python3 -B -m unittest discover -s desk-tools/fanout
python3 -B -m unittest test_sync_deploy && python3 sync.py --check
python3 -B image/test_fetch_gate.py
sudo bash console/test_netns.sh                               # real nftables in throwaway namespaces (Linux, root)
```

None of these needs the two machines; the smoke tests above are what check a real install.

## Honest limits

- The sandbox reboot between jobs clears RAM and tmpfs, not its disk, and it is off on the current deployment (a reboot per
  job is too slow): jobs reuse the sandbox as the last job left it, each still in a fresh `bwrap`.
- The desk's stubs are not hash-pinned in their contracts yet (`integrity.run_sha256`).
- Restarting pi-bridge ends every open Pi session.
- In lite mode the core and the sandbox share a kernel. Nothing here protects data you hand the agent.

Open work is in `TODO.md`. Licence: MIT (`LICENSE`). The bundled DeskBox binaries carry the licence of the
`ralabarge/deskbox` commit they were built from.
