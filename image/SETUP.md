# Enclave: setup and use

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
>   `bwrap` with no network unless its `tools.list` line opts in. Convenient, not the locked-down option.
> - **Full mode:** the sandbox runs on its own device, as above. See below.

## Boot and log in

1. Boot the VM. Services start on their own: `ollama`, `loom`, `sbx-console`, `sbx-broker`, `sbx-session-gate`, `deskbox`,
   `tailscaled` (no account until you run `tailscale up`), `nftables`, `ssh`.
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
- **DeskBox** (`/usr/local/bin/agent-desk`, desk at `http://127.0.0.1:8080`): the desk every tool call goes through.
  It runs as the `deskbox` system account under `-strict`, serves the tools in `/opt/deskbox/tools` (0700, that account only),
  and keeps its job store and workspaces in `/var/lib/deskbox`. `systemctl status deskbox` and `curl -s 127.0.0.1:8080/`
  are the two things to look at: the second prints the `enforcement` block, which says whether the sandbox, the syscall filter
  and the per-job limits are actually live rather than merely configured.
  The broker's socket is `/run/sbx/broker.sock` (mode 0660, group `sbxdesk`). That socket, bound into each job by the contract's
  `sandbox.bind`, is the only way a tool call leaves the desk, and no stub has a network at all.
- **sbx-session-gate** (`/run/sbx-gate/gate.sock`): the one place a new session's firewall profile is forced. The bridge calls it
  before starting Pi, and refuses to start a session if it cannot. Only the gate can read `/etc/sbxfw/tighten-token`, and the only
  action it exposes is "make sure the session profile is active" — there is no endpoint that loosens anything.

The bridge can also log each settled turn to Loom: start it with `--loom-url http://127.0.0.1:9741` (off by default, and the
shipped unit does not set it). Each turn goes to `/log` as one assistant message holding the assistant text, the tool calls and
results, and any Pi event the bridge does not recognise. One message is deliberate: `/log` stores only the last user message and
the first assistant content, so splitting the transcript across messages would record turns that never happened.

**One lane for every harness.** All broker callers share one serial lane: a job runs, `SBX_RESET_CMD` runs, then
the next starts. A call arriving while a job runs waits in order for up to `SBX_QUEUE_WAIT_S`; if `SBX_QUEUE_MAX`
callers already wait, or that wait expires, the broker answers `503` (`lane_busy`, with `queued`/`depth_max`) instead
of hanging. Raise both settings for more harnesses; lower them to fail fast. `GET /status` reports `busy`, `queued`,
`depth_max`, the last job, the `reset` state (`ok`, `rc`, `at`) and `"lane": "serial"`; a failed reset makes the next
call retry it and, if that also fails, answer `503 sandbox_not_reset`.

## Adding a tool

Tools are not installable from the console. Add one by hand:

1. Add a line to `tools.list`: `name [timeout_seconds] [net]` (timeout default 30, max 120). Add `net` only if the tool
   needs the sandbox's network namespace; without it the tool runs with no network. The console's Tools tab can toggle it later.
2. Write `sbx/tools/<name>/run` (runs on the sandbox side; reads JSON on stdin, writes JSON on stdout).
3. Write `harness/tools/<name>-sbx/tcs.yaml` (the contract, on the trusted side). It must keep
   `allowed_side_effects.network: false` and declare the broker socket in `sandbox.bind`; `sync.py` refuses a contract that
   drops either, because a stub with no socket and no network cannot call anything.
4. Run `python3 sync.py`. It generates the stub and validates everything.
5. Deploy: `tools.list` goes to `/etc/sbx/tools.list` (harness) and to the sandbox side's `tools.list`
   (see Known limits). Copy `sbx/tools/<name>` to `/opt/sbx/tools/`. Then
   `sudo python3 sync.py --deploy /opt/deskbox/tools && sudo chown -R deskbox:deskbox /opt/deskbox/tools` — the desk only
   serves what is in that directory, and 0700 on the run scripts is what stops anything but the desk from executing them.
6. Pin the stub: `agent-desk -pin /opt/deskbox/tools/<name>-sbx`, then paste `integrity.run_sha256` into the repo's
   `tcs.yaml` and re-deploy. The desk checks the pin at load and again per job.

If `tcs-verify` is on `PATH`, `sync.py` runs it over every contract and fails on what it finds; it is the only thing here
that parses the schema the way the gate does, so it is what notices a keyword the validator would silently drop (a
`default:` in a property, for instance). In a checkout that cannot store the exec bit, `tcs-verify` also warns that
`run.py` is group/other-readable: the deployed copy is written 0700, which is the mode the desk actually checks.

The broker and `sbx-run` re-read their `tools.list` on each request, so list edits apply without a restart. A network
tool still only reaches what the Firewall tab allows for the sandbox.

### `rsync` allowlist

`rsync` is listed as `rsync 120 net`; the sandbox image installs `rsync` and `openssh-client` (`image/provision-sbx.sh`). Its
allowlist is a file in the tool's own directory on the sandbox side: `/opt/sbx/tools/rsync/allowlist` (inside the job,
`/deskbox/tool/allowlist`). Start from `allowlist.example` and keep one entry per line:

```text
# <name> = [user@]<host>[:port]
backup = ryan@backup.example.com
nas = 10.0.0.10:22
```

A remote source or destination is written `<name>:<path>`; the name must match an entry exactly, and the user and port come
only from this file. There are no wildcards, CIDR ranges or default entry: an empty, missing or malformed allowlist permits
no remote host. The local side is limited to `/deskbox/in`, `/deskbox/out` and `/tmp`, so the job cannot upload the allowlist
or the SSH key from its own tool directory. The call runs `rsync --dry-run` unless it sets `"apply": true`. SSH reads
`id_ed25519` and `known_hosts` from that same directory (neither is in the repo), with `StrictHostKeyChecking=yes` and
`BatchMode=yes`, so an unknown host or a passphrase fails instead of prompting. A `net` tool now also gets
`/etc/resolv.conf`, `/etc/passwd` and the trust store bound in (`sbx/sbx-run`), so a hostname in the allowlist resolves.
Keep the allowlist and key readable by the sandbox's `sbx` account (the job runs as it; the key should be mode 600), and
copy the run script executable: `chmod 755 /opt/sbx/tools/rsync/run`.

## A second harness

Point the harness's tool definitions at the desk:

```sh
python3 /usr/local/lib/enclave/desk_tools.py --desk http://127.0.0.1:8080 openapi
```

That file is installed by `provision.sh`; in a checkout it is `harness/toolsadapter/desk_tools.py`. `list` and `text` are
the other output forms. The desk is the only executor: every call goes back to `POST /tools/{name}`, so the harness needs no
tool code. Do not give the harness a shell — that gives up the contract, the sandbox and the account boundary at once; see
`docs/HARNESS-ADMISSION.md`.

## Full mode: the sandbox on its own device (the strongest setup)

The sandbox is a second machine that knows only this core (`10.77.0.1`). The core is its gateway: it forwards the sandbox's traffic
only as far as the allowlist says, and masquerades it out. The sandbox holds none of the core's software or secrets.

**Layout:** core `eth0` = the real network, core `eth1` = `10.77.0.1/24` (private link), sandbox `eth0` = `10.77.0.2/24`
(the private link and nothing else). The sandbox's DNS is `10.77.0.1`; the core redirects it to the resolver you list.

**On the Windows PC (verified):** QEMU runs both VMs and joins them with a socket network, so the sandbox has no other path.

```powershell
enclave.cmd            # menu: build, start both VMs, stop, reset. The core's own window is its console.
```

**On the core:** `sudo sbx-mode box` points the broker at `10.77.0.2` and turns this VM's own sshd off. `sudo sbx-mode lite` goes back.
Then allow what the sandbox needs in the console's Firewall tab (resolver first, then host:port rules).

**On the sandbox:** `sudo sbx-node-test` checks it (default expects every outbound request to be blocked; after allowing a host on
the core, `sudo sbx-node-test --expect open <host>`). `sudo sbx-smoke-test` on the core checks the other half, including the
greet tool running through the broker into the sandbox.

On a physical sandbox box the same layout applies with a real second NIC pair or a crossover cable; set the core's `SBXC_SBX_IFACES`
(default `eth1`) to the NIC that faces it.

**Per-job reset.** The broker runs `SBX_RESET_CMD` after every job while holding its one-job lock, and the next job starts as
soon as that command returns. A reset that returns early is worse than none: the broker would hand out work on a box in an
unknown state. The shipped choices:

| Mode | `SBX_RESET_CMD=` | What it does |
|---|---|---|
| lite | *(empty)* | Nothing to reset. Every job already gets a fresh `bwrap` tmpfs, and the sandbox is this VM, so a reboot would take the broker with it. |
| full, separate box | `/usr/local/bin/sbx-reboot-and-wait` | Reboots the box over the broker's SSH channel and waits for that channel to answer again. Installed on the core by `provision.sh`; the box side is installed by `provision-sbx.sh`. |
| full, QEMU VM, disk revert | **not shipped:** leave it empty | `sbx-vm-revert` is a host-side QEMU operation. See below. |

`sbx-reboot-and-wait` sends the reboot through `/opt/sbx/sbx-channel`: the same key, account, port and known_hosts the broker
uses, with one extra verb. It then waits up to 60 s for the box to drop SSH and up to 180 s more for `sbx-run` to answer an
empty request again (exit 2 = the tool channel is back). If either deadline passes it prints the reason to stderr and exits 1;
the broker logs the failure. It clears RAM and tmpfs, not the box's disk. The box needs `sudo`, `/usr/local/sbin/sbx-reset` and
the sudoers line; `provision-sbx.sh` installs all three. On a hand-built box, copy `sbx/sbx-channel` and `image/sbx-reset.sh`
and add the sudoers line as `provision-sbx.sh` does. A box image built before this change has no channel: the reboot request
then fails with an SSH exit status and the broker logs it, rather than reporting a reset that did not happen. Rebuild the box
image with `provision-sbx.sh`, or install those three pieces by hand.

**`sbx-vm-revert`: what it must be, and why it is not here.** Reverting a VM disk snapshot is a host-side QEMU operation: stop
the sandbox VM, throw away its overlay (`build\run\sbx.qcow2`), re-create it from the built image
(`build\packer-out\sbxnode\sbx-node.qcow2`), and start it again. The broker runs `SBX_RESET_CMD` on the core, and the core has
no channel to the Windows hypervisor. A script installed on the core under that name could only be a no-op or a failure; either
way the disk is not reverted, which is the fake this setup deliberately does not ship. The launcher already creates a fresh
sandbox overlay on every start, so stop/start is the clean-disk operation today. If you add a host-side channel, the helper it
calls must meet the same contract as `sbx-reboot-and-wait`: bounded, non-zero on failure, and it must not return until the
box's SSH answers again. That helper cannot be written and called correctly without testing on the host, so it is documentation
here, not code. The console's reset dropdown still offers `/usr/local/bin/sbx-vm-revert`; choosing it while the file is absent
makes every reset fail (the broker logs it and the sandbox is not reset). Leave the setting empty on a VM unless you have
installed a host-side helper and a way for the core to call it.

Notes: the sandbox's `/etc/resolv.conf` is immutable (`chattr -i` before changing it). Tools in the sandbox run in `bwrap` with no
network unless their `tools.list` line says `net`; a network tool shares the sandbox's network namespace and reaches only what the
core's firewall forwards for the sandbox.

## Tailscale and bridge peers (installed, off by default)

The image has `tailscaled` and `tailscale` (pinned in `image/manifest.env`) and `tailscaled.service` is enabled, but
this node is **not logged in**: no auth key or account is baked in, the same rule as the Bitwarden token. Peer
messaging is off as well: `pi-bridge.service` starts without `--peers`, so it opens no peer listener.

To join your tailnet, on the core as `sbxadmin`:

```sh
sudo tailscale up --accept-dns=false   # prints a login URL; approve this node on any device on your tailnet
```

`--accept-dns=false` keeps tailscaled from rewriting `/etc/resolv.conf` (its unit runs with `ProtectSystem=strict`);
the bridge talks to peers by tailnet IP, so MagicDNS is not needed. `tailscale status` then shows the tailnet and
`sudo tailscale down` leaves it. Node identity lives in `/var/lib/tailscale`; `sudo systemctl disable --now tailscaled`
takes the agent out of boot. Nothing here is required for the single-machine setup.

To turn peer messaging on, add `--peers` to the bridge's `ExecStart` and restart it:

```sh
sudo systemctl edit --full pi-bridge     # append  --peers  to the ExecStart line
sudo systemctl restart pi-bridge
```

The peer listener binds this node's tailnet address on TCP 9761 only; the local API stays on `127.0.0.1:9760`. Peers
are still refused by the firewall's default-deny input chain, because the console's generated ruleset has no
`tailscale0` rule: `console/nftables.conf` records the one interface-scoped rule that is missing and why it cannot
live in that file (the console rewrites `/etc/sbxfw/ruleset.nft` on every Apply). Until that rule is in the console's
generated input chain, inbound peer connections to 9761 are dropped and logged as `sbxfw-drop`.

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
- **`sbx-vm-revert` is not shipped.** A VM disk revert is a host-side QEMU operation, but the broker runs `SBX_RESET_CMD` on
  the core. The shipped full-mode hook is `sbx-reboot-and-wait` (a reboot, not a disk revert). See Full mode for the contract.
  The console's reset dropdown still offers the `sbx-vm-revert` path; choosing it without the file makes the reset fail.
- **Provisioning gaps (check before relying on tools):** `provision.sh` installs `tools.list` only to
  `/etc/sbx/tools.list`, but `sbx-run` reads `/opt/sbx/tools.list`, which it does not install. It also does not
  create the `sbx` SSH user, does not enable `sbx-broker`, and does not write the forced-command key or set
  `SBX_HOST` (both are manual steps it lists). Until these are fixed, the greet check will FAIL.

## Secrets (Bitwarden Secrets Manager)

The core fetches its LLM keys (`OPENROUTER_API_KEY`, `REQUESTY_API_KEY`) from Bitwarden when `pi-bridge` starts. Nothing secret is in the image or the repo.

In the web vault (free tier is enough): enable Secrets Manager for the org, make a project `enclave`, add the secrets there with `UPPER_SNAKE_CASE` names, make a machine account with **read-only** access to that project, and create an access token for it.

On the core VM, as root:

```bash
echo 'BWS_PROJECT_ID=<project uuid>' > /etc/sbx/bws.env
read -rs T && printf '%s' "$T" > /etc/sbx/bws-token; unset T      # paste the token, it is not echoed
chown root:root /etc/sbx/bws.env /etc/sbx/bws-token; chmod 600 /etc/sbx/bws.env /etc/sbx/bws-token
systemctl restart pi-bridge && sbx-smoke-test
```

How it works: `pi-bridge.service` runs `sbx-fetch-secrets` as root (`ExecStartPre=+`) before the service user's process exists. It reads the token, asks Bitwarden for the project's secrets, and writes them to `/run/sbx/secrets.env` (tmpfs, `root:pibridge 0640`); the service loads that file with `EnvironmentFile=`. No token file means an empty env file and the bridge starts without keys. A token Bitwarden rejects, a secret name that is not a valid variable name, or no network stops the service from starting.

What Pi can reach: the fetched keys (they are in its environment and in `/run/sbx/secrets.env`). It cannot read the access token (`/etc/sbx/bws-token` is `root:root 0600`), so a compromised agent can leak those keys but cannot go on to read other secrets in the project, and cutting access is: remove the key in the vault, then `systemctl restart pi-bridge`. Keep the project to the keys the core needs.
