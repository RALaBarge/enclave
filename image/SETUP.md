# sbx VM: setup and use

> **WARNING: this VM is not a security boundary.**
> A VM limits what a misbehaving agent or tool can do. It does not make it safe. A hypervisor or VM escape
> lands on the host. Run this only on a **disposable machine with no route to anything you care about**
> (no shared folders, no mounted disks, no LAN access to real systems, no stored credentials you need).
> Keep the firewall in default-deny. Also enforce egress at the hypervisor or network edge, so the VM cannot
> reach out even if its own firewall is misconfigured.
>
> **The strongest setup:** the sandbox on its own device that knows only this core's address, with the core as its
> gateway deciding what its traffic may reach. Set it up exactly that way (Full mode, below).
>
> - **Lite mode (default):** the sandbox runs inside this VM, sharing one kernel with the core. Each tool runs in its own
>   `bwrap` with no network. Convenient, not the locked-down option.
> - **Full mode:** the sandbox runs on its own device, as above. See below.

## Boot and log in

1. Boot the VM. Services start on their own: `ollama`, `loom`, `sbx-console`, `sbx-broker`, `nftables`, `ssh`.
2. Log in on the VM console as `sbxadmin` with the initial password `sbx-change-me`. You are forced to set a new
   password immediately. Nothing on the network can use the initial password: sshd listens on 127.0.0.1 only and
   admits only the `sbx` forced-command user. Everything else listens on loopback (127.0.0.1 / ::1) only too.
3. Check that everything came up: `sudo sbx-smoke-test` (see Verify below).

## Console (firewall, tools, settings)

- Open `http://127.0.0.1:8787` in a browser inside the VM.
- Get the bearer token (the page asks for it):
  ```sh
  sudo cat /etc/sbxfw/token
  ```

## Firewall: what the sandbox may reach

This VM is the sandbox's **gateway**. The rules apply to traffic the sandbox sends *through* this VM (arriving on `eth1`, the private
link). The fresh install forwards **nothing**. This VM's own traffic is not restricted by them.

**Two profiles, one active at a time.** `session` is what the agent may reach while it works (for example your LLM API host).
`build` is wider, for installing things (package registries, git hosts). Each profile has an **allow list** and a **block list**; a block
always wins over an allow, so you can allow a whole range and carve out one host (say the cloud metadata address). In the **Firewall** tab:

1. Pick *Session rules* or *Build rules* to edit, add allow and block entries, and click **Apply**.
2. **Apply is instant** and saved at once. Allow and block edits and profile switches cannot lock you out, because they only affect the
   sandbox's traffic. Only a change to *management access* (who may reach this VM) keeps the 60-second confirm: it goes live, then
   **reverts after 60 seconds unless you confirm**.
3. **Switch to build** takes a time limit (30 minutes, 2 hours, or none). When it runs out the console switches back to session by itself,
   so a build window cannot be left open. From the shell: `POST /api/fw/profile {"active":"build","for_s":1800}`.

| Example allow        | Port | Why                         |
|----------------------|------|-----------------------------|
| your LLM API host    | 443  | model calls (session profile) |
| `pypi.org`, `github.com` | 443 | installing packages (build profile) |
| your DNS resolver    | 53   | the sandbox asks this VM for DNS; the first resolver listed answers (shared by both profiles) |

Hostnames resolve when you click Apply. CDN hosts rotate IPs, so re-apply if a service stops working. Blocks are logged to the Blocked
attempts table like any other drop. If the console is down when a build window ends, it switches back to session as soon as it starts;
until then the build rules stay in force.

## Pi, Loom, DeskBox

All three are local to the VM.

- **Pi** (`/usr/local/bin/pi`): the lightweight agent harness. Run `pi --version` to confirm it works. For usage, see
  its own docs at the pinned commit in `image/manifest.env`.
- **Loom** (`http://127.0.0.1:9741`): memory service. It embeds text through Ollama using `nomic-embed-text`
  (`http://127.0.0.1:11434`). Embeddings need no internet access.
- **DeskBox** (`/usr/local/bin/deskbox`, desk at `http://127.0.0.1:8080`): the desk the agent works in.
  Run `deskbox -h` for its options.

## Adding a tool

Tools are not installable from the console. Add one by hand:

1. Add a line to `tools.list`: `name [timeout_seconds]` (default 30, max 120).
2. Write `sbx/tools/<name>/run` (runs on the sandbox side; reads JSON on stdin, writes JSON on stdout).
3. Write `harness/tools/<name>-sbx/tcs.yaml` (the contract, on the trusted side).
4. Run `python3 sync.py`. It generates the stub and validates everything.
5. Deploy: `tools.list` goes to `/etc/sbx/tools.list` (harness) and to the sandbox side's `tools.list`
   (see Known limits). Copy `sbx/tools/<name>` to `/opt/sbx/tools/`.
6. Re-pin the stub hash in agent-desk. The Tools tab in the console shows the hash.

The broker re-reads `/etc/sbx/tools.list` on each request, so list edits apply without a restart.

## Full mode: the sandbox on its own device (the strongest setup)

The sandbox is a second machine that knows only this core (`10.77.0.1`). The core is its gateway: it forwards the sandbox's traffic
only as far as the allowlist says, and masquerades it out. The sandbox holds none of the core's software or secrets.

**Layout:** core `eth0` = the real network, core `eth1` = `10.77.0.1/24` (private link), sandbox `eth0` = `10.77.0.2/24`
(the private link and nothing else). The sandbox's DNS is `10.77.0.1`; the core redirects it to the resolver you list.

**On the Windows PC (verified):** QEMU runs both VMs and joins them with a socket network, so the sandbox has no other path.

```powershell
bash image/gen-keys.sh                                  # once: broker key + sandbox host key (build/keys, gitignored)
# build both images (see image/packer/README.md): -only='sbx-vm.qemu.sbx' and -only='sbx-node.qemu.sbxnode'
powershell -File image\run-two-vm.ps1                   # boots both; consoles on VNC 5907 (core) and 5908 (sandbox)
powershell -File image\run-two-vm.ps1 -Stop             # stops both, removes the overlays
```

**On the core:** `sudo sbx-mode box` points the broker at `10.77.0.2` and turns this VM's own sshd off. `sudo sbx-mode lite` goes back.
Then allow what the sandbox needs in the console's Firewall tab (resolver first, then host:port rules).

**On the sandbox:** `sudo sbx-node-test` checks it (default expects every outbound request to be blocked; after allowing a host on
the core, `sudo sbx-node-test --expect open <host>`). `sudo sbx-smoke-test` on the core checks the other half, including the
greet tool running through the broker into the sandbox.

On a physical sandbox box the same layout applies with a real second NIC pair or a crossover cable; set the core's `SBXC_SBX_IFACES`
(default `eth1`) to the NIC that faces it. Per-run reset: set `SBX_RESET_CMD` in `/etc/sbx/broker.env` to a reset that restores
the sandbox after each job. **The reset hooks (`sbx-vm-revert`, `sbx-reboot-and-wait`) are not shipped.**

Notes: the sandbox's `/etc/resolv.conf` is immutable (`chattr -i` before changing it). Tools in the sandbox still run in `bwrap`
with no network of their own; a tool that needs the gateway path would need a per-tool network flag (see `TODO.md`).

## Verify

```sh
sudo sbx-smoke-test                # full check, including the greet call through the broker
sudo sbx-smoke-test --quick  # skips the core-egress probe and the greet call
```

Each check prints PASS, FAIL or SKIP with a reason. The script exits 1 if anything FAILs. Run it after install,
after every firewall change, and after any tool change.

## Known limits

- **Egress is by hostname at apply time.** Rotating CDN IPs can break allowed services until you re-apply.
- **The revert timer lives in the console process.** If the console crashes during the 60-second window, the
  unconfirmed rules stay until reboot.
- **Blocked-attempt log** (Firewall tab) is empty if the console user cannot read the journal.
- **Lite mode is not a boundary.** The `bwrap` jail protects the sandbox box from tools. It does not protect this
  VM from a hostile tool that escapes `bwrap`.
- **Reset hooks are not shipped.** Lite mode does not need one. Full mode does.
- **Provisioning gaps (check before relying on tools):** `provision.sh` installs `tools.list` only to
  `/etc/sbx/tools.list`, but `sbx-run` reads `/opt/sbx/tools.list`, which it does not install. It also does not
  create the `sbx` SSH user, does not enable `sbx-broker`, and does not write the forced-command key or set
  `SBX_HOST` (both are manual steps it lists). Until these are fixed, the greet check will FAIL.
