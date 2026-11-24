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
| `console/` | the **console** (`127.0.0.1:8787`, bearer token): firewall (session and build profiles, allow and block lists, DNS, management access, blocked attempts), tools (enable, timeout, the code and contract behind each), activity (the desk's recent jobs with request and result), memory (review queue: accept, edit or reject proposed memories; retract), settings (read-only). `test_netns.sh` proves the rules on a real kernel. |
| `harness/gate/` | **session gate**: the one place a new session's firewall profile is forced. Holds the tighten-only token and exposes nothing that loosens. |
| `harness/broker/` | **broker**: one job lane to the sandbox over SSH (forced command), on a unix socket only the desk's group may use. Locks the firewall to session before any `net` job and can reboot the sandbox between jobs. |
| `harness/stub/`, `harness/toolsadapter/` | the stub every `-sbx` tool runs under DeskBox (talks to the broker, nothing else), and the adapter that turns desk contracts into a harness's tool definitions, including how much of a result goes inline. |
| `harness/loomship/`, `harness/proposer/`, `harness/redact/` | **memory**: `loomship` ships Pi's session files to Loom, redacted, and moves its cursor only on Loom's receipt; the proposer reads finished sessions and queues candidate memories for review; the redactor both use. |
| `deskbox` (pinned in `image/manifest.env`) | the **desk** every tool call goes through: contract check in, contract check out, each run in its own bwrap, as its own account. Binaries in `image/artifacts`. |
| `tools/` | the sandbox tools: the [DeskboxTools](https://github.com/RALaBarge/DeskboxTools) submodule (`tools.list`, and per tool its `run` and contract). Clone with `--recurse-submodules`. |
| `desk-tools/` | tools that run on the core under the desk instead of on the sandbox, because they need a secret or a core service the sandbox must never reach: `fanout` (ask several models, one synthesises) and `memq` (propose to, pop from and search memory). |
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

## Memory

Loom keeps every entry Pi writes, as an append-only event: `loomship` reads Pi's session files, redacts every string, and
posts them; its cursor moves only past entries Loom confirms it committed, so nothing is lost while Loom is down. Tool output is
stored marked untrusted: kept for audit, never embedded, never recalled. Your prompts and the assistant's final replies are
embedded for search.

Nothing becomes memory without you. Candidates come from the proposer (every 10 minutes it reads sessions idle for 30 and asks
the model what is worth keeping, from trusted text only), from the agent (`memq push`), or from you (`memq push` on the core).
They wait in the console's **Memory** tab, the only place that can accept one (it alone holds Loom's review token). `memq pop`,
run by the agent or by you, moves accepted entries into memory; `memq search` looks them up. Memory reaches a session only when
asked for that way. Each writer has its own account and Loom token: `loomship` (ingest), the proposer and `memq` (propose, pop),
the console (review). `harness/proposer/calibrate.py` scores the proposer against labelled windows, injections included; run it
after any model or prompt change.

Secrets come from Bitwarden Secrets Manager at service start: the model keys go to pi-bridge's environment through a file in
`/run` (tmpfs), and the gate token only into the `page` and `fetch` tool directories (root:sbx, 0640) that are pushed to the
sandbox. The core keeps only the read-only Bitwarden access token on disk (root, 0600). See `image/SETUP.md`, "Secrets".

## Install

### What you need

- **Two x86-64 machines** with a fresh Debian 13 install. Small is fine: the running core is a 2-core thin client with 3.4 GB
  RAM and a 15 GB disk (about 0.5 GB RAM and 5 GB disk used). The sandbox needs less.
- **A second network port on the core** (a USB Ethernet adapter works) and a cable from it to the sandbox.
- **A Tailscale account**, for reaching the core.
- **Bitwarden Secrets Manager** (the free tier is enough) holding your model API keys (`DEEPSEEK_API_KEY`,
  `OPENROUTER_API_KEY`, or any `*_API_KEY`).
- **A workstation** with `git`, `ssh-keygen`, `rsync` and Go 1.26+ (Go only to build DeskBox).

### 1. On your workstation: get the code, the keys and DeskBox

```sh
git clone --recurse-submodules https://github.com/RALaBarge/enclave.git && cd enclave
bash image/gen-keys.sh        # build/keys/core and build/keys/sbx; build/ is gitignored, keep it private
```

`image/artifacts/` holds the three DeskBox binaries and is not committed. Build them from the commit pinned in
`image/manifest.env` (`DESKBOX_COMMIT`), as `image/BUILD-DESKBOX.md` shows:

```sh
git clone https://github.com/ralabarge/deskbox /tmp/deskbox && git -C /tmp/deskbox checkout <DESKBOX_COMMIT>
for b in agent-desk tcs-shim tcs-verify; do
  (cd /tmp/deskbox && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o "$OLDPWD/image/artifacts/$b" ./cmd/$b)
done
```

### 2. The sandbox: provision it while it is still on your LAN

Each machine gets only its own half of the keys: the sandbox must never see the broker's private key. Its network port
must be named `eth0` (the scripts' network config and firewall use that name); if Debian named it `enp...`, pin it with a
`.link` file like the one in step 3 (`Name=eth0`) and reboot first.

```sh
rsync -a --exclude build/keys/core --exclude build/keys/.src ./ <you>@<sandbox>:enclave/
ssh -t <you>@<sandbox> 'cd enclave && sudo bash image/provision-sbx.sh && sudo bash image/finalize-sbx.sh && sudo poweroff'
```

`finalize-sbx.sh` switches the box to its final network (static `10.77.0.2`, the core as gateway and DNS, SSH only from the
core's broker) from the next boot on. After it powers off, move its cable from the LAN to the core's second port and power it on.
From then on the only way in is its keyboard, or the broker's forced command from the core.

### 3. The core

```sh
rsync -a --exclude build/keys/sbx --exclude build/keys/.src ./ <you>@<core>:enclave/
ssh -t <you>@<core>
sudo mv ~/enclave /srv/enclave && cd /srv/enclave
sudo bash image/provision.sh 2>&1 | sudo tee /root/provision.log     # Loom, Ollama, Pi built from source, DeskBox, console
```

The port that faces the sandbox must be named `eth1` (or set `SBXC_SBX_IFACES` for the console). A USB adapter gets a
name like `enx<mac>`; pin it with a `.link` file, then reboot:

```ini
# /etc/systemd/network/10-sbx-link.link
[Match]
MACAddress=<the adapter's MAC>
[Link]
Name=eth1
```

### 4. Your way in: an operator account and Tailscale

sshd on the core listens on loopback only and admits `sbxadmin` (you, by key) and `sbx` (the broker). Make the account,
join the tailnet, and forward the tailnet's port 22 to that loopback sshd:

```sh
sudo useradd -m -s /bin/bash -G sudo sbxadmin && sudo passwd sbxadmin     # the password is for sudo; SSH is key-only
sudo install -d -m 700 -o sbxadmin -g sbxadmin /home/sbxadmin/.ssh
echo '<your public key>' | sudo tee /home/sbxadmin/.ssh/authorized_keys && sudo chown sbxadmin: /home/sbxadmin/.ssh/authorized_keys
sudo tailscale up --accept-dns=false                       # prints a login URL; approve the node
sudo tailscale serve --bg --tcp 22 tcp://127.0.0.1:22
```

From now on: `ssh sbxadmin@<core's tailnet name>`.

### 5. Secrets

In the Bitwarden web vault: enable Secrets Manager, make a project `enclave`, add your API keys there, make a machine account
with **read-only** access to that project and create an access token for it. Then on the core:

```sh
echo 'BWS_PROJECT_ID=<project uuid>' | sudo tee /etc/sbx/bws.env
read -rs T && printf '%s' "$T" | sudo tee /etc/sbx/bws-token >/dev/null; unset T    # paste the token, it is not echoed
sudo chmod 600 /etc/sbx/bws.env /etc/sbx/bws-token
sudo systemctl restart pi-bridge
```

### 6. Point the broker at the sandbox, push the tools, check it

```sh
sudo sbx-mode box          # broker -> 10.77.0.2, using the sandbox host key pinned at install
sudo sbx-push-tools        # tools.list, sbx-run and every tool, over the broker's channel
sudo sbx-smoke-test        # on the core: every check should pass
```

On the sandbox, at its keyboard: `sudo sbx-node-test` (every outbound attempt blocked is the pass).

Then open the UI and the console through the tunnel (see **Using it**), and in the console's Firewall tab add a DNS resolver
and whatever the sandbox's `net` tools need. Everything starts closed.

### Optional

- **`page` and `fetch`:** run browserbox's gate on another machine, add `SBX_GATE` = `http://<host>:9011 <token>` to the
  Bitwarden project, allow `<host>` TCP 9011 in both firewall profiles, then `sudo sbx-push-tools`.
- **`fanout`:** write its `keys.json` on the core as described in `desk-tools/README.md`.

### Other ways to run it

- **Two VMs on Windows:** `enclave.cmd` (menu: build images, start, stop, reset). Packer + QEMU with WHPX; see
  `image/packer/README.md`. Builds need a Windows checkout (WSL has no `/dev/kvm`).
- **Lite mode:** one machine, the sandbox is `bwrap` on the core itself (`sudo sbx-mode lite`). Convenient, not locked down.

The full operator guide, including how the firewall, the reset between jobs and adding tools work, is `image/SETUP.md`.

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
python3 -B -m unittest discover -s desk-tools/memq
python3 -B -m unittest discover -s harness/loomship
python3 -B -m unittest discover -s harness/proposer
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
