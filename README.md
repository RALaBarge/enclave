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
| `harness/bridge/` | **pi-bridge**: HTTP/SSE API over Pi's RPC mode, one Pi process per session; optional tailnet peer messaging. Forces the `session` firewall profile before any session starts. |
| `console/` | **console**: the firewall (profiles, allow and block lists, instant apply, build timer), the tools list, settings; a small web page. `test_netns.sh` proves the rules on a real kernel. |
| `harness/broker/` | **broker**: sends tool calls from the core to the sandbox over SSH (forced command). |
| `sbx/` | the sandbox side: `sbx-run` and the tools, each in its own `bwrap` with no network. |
| `image/` | builds the VM images with Packer + QEMU: the **core** (`sbx-vm`) and the lean **sandbox** (`sbx-node`, no secrets), plus `run-two-vm.ps1` to boot both. |

Code identifiers keep the older `sbx` name (`sbx-console`, `/opt/sbx`, the `sbxfw` firewall table); Enclave is the project.

## Try it

```powershell
bash image/gen-keys.sh                       # once
# build both images: image/packer/README.md
powershell -File image\run-two-vm.ps1        # boots core + sandbox on QEMU
```

Then follow `image/SETUP.md`. On the core: `sudo sbx-mode box`, `sudo sbx-smoke-test`. On the sandbox: `sudo sbx-node-test`.

## Tests

```
python console/test_console.py                      # 28 tests
python -B -m unittest discover -s harness/bridge/tests   # 17 tests
bash console/test_netns.sh                          # 28 checks, real nftables, throwaway namespaces (WSL/Linux, root)
```

## Honest limits

A VM is not a hardware boundary: the strongest setup runs the sandbox on its own device. Lite mode (one VM) shares a kernel between
core and sandbox. Nothing here protects data you hand the agent. See `image/SETUP.md` and `TODO.md` for status and open work.

Licence: MIT (`LICENSE`). The bundled DeskBox binaries carry the licence of the `ralabarge/deskbox` commit they were built from.
