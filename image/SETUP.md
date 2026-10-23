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
   admits `sbx` (the broker's forced command) and `sbxadmin` (your key-only operator login). Everything else listens
   on loopback (127.0.0.1 / ::1) only too.
3. Check that everything came up: `sudo sbx-smoke-test` (see Verify below).

## Console (firewall, tools, settings)

- Open `http://127.0.0.1:8787` in a browser inside the VM.
- Get the bearer token (the page asks for it):
  ```sh
  sudo cat /etc/sbxfw/token
  ```
- The Settings tab shows `broker.env` read-only; `sbx-mode` owns it. The reset checkbox writes
  `/etc/sbxfw/reset-off`, which the broker checks before every routine reset.

## Firewall: what the sandbox may reach

This VM is the sandbox's **gateway**. The rules apply to traffic the sandbox sends *through* this VM (arriving on `eth1`, the private
link). The fresh install forwards **nothing**. This VM's own traffic is not restricted by them.

**Two profiles, one active at a time.** `session` is what the sandbox may reach while the agent works, and it starts and should usually stay empty. The model is
called from this VM, not from the sandbox, so the sandbox never needs your LLM API host: every allowed host is somewhere a
tool can send data.
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
| `pypi.org`, `github.com` | 443 | installing packages (build profile) |
| your DNS resolver    | 53   | the sandbox asks this VM for DNS; the first resolver listed answers (shared by both profiles) |

Hostnames resolve when you click Apply. CDN hosts rotate IPs, so re-apply if a service stops working. Blocks are logged to the Blocked
attempts table like any other drop. Every new connection the sandbox makes through an allow or DNS rule is logged too, so an
exfiltration to an allowed host leaves a trace: `journalctl -k --grep sbxfw-accept` (rate-limited to 10 per second). If the console is down when a build window ends, it switches back to session as soon as it starts;
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
- **pi-bridge session roots**: the unit passes `--cwd-root /var/lib/pi-bridge-work`; every session cwd must resolve under one of those roots, and sessions outside them cannot be created or restored (old records come back `refused`, not dropped).

The bridge can also log each settled turn to Loom: start it with `--loom-url http://127.0.0.1:9741` (off by default, and the
shipped unit does not set it). Each turn goes to `/log` as one assistant message holding the assistant text, the tool calls and
results, and any Pi event the bridge does not recognise. One message is deliberate: `/log` stores only the last user message and
the first assistant content, so splitting the transcript across messages would record turns that never happened.

**One lane for every harness.** All broker callers share one serial lane: a job runs, its reply goes out,
`SBX_RESET_CMD` runs with the lane still held, then the next starts. A call arriving while a job runs waits in order for up to `SBX_QUEUE_WAIT_S`; if `SBX_QUEUE_MAX`
callers already wait, or that wait expires, the broker answers `503` (`lane_busy`, with `queued`/`depth_max`) instead
of hanging. Raise both settings for more harnesses; lower them to fail fast. `GET /status` reports `busy`, `queued`,
`depth_max`, the last job, the `reset` state (`ok`, `rc`, `at`) and `"lane": "serial"`; a failed reset makes the next
call retry it and, if that also fails, answer `503 sandbox_not_reset`.

## Page and fetch (browserbox gate)

`page` and `fetch` reach Ryan's browser through browserbox's gate on the Windows host. The gate enforces a host
allowlist (`browserbox/gate.hosts`: job boards and ATS sites only) before it opens a page or makes a request, checks
the tab's final URL before returning content, and refuses fetch redirects. Run the gate from a Chrome profile signed
in only to those sites, and after every extension change confirm that a redirect to an off-list host is refused
before the token is enabled.

The gate address and bearer token are the Bitwarden secret `SBX_GATE` in the same project as the model keys:
`http://HOST:9011 TOKEN`, token 32-128 non-space characters. `sbx-fetch-gate` (root) writes
`/opt/sbx/tools/{page,fetch}/gate` as `0640 root:sbx`; `sbx-push-tools` refreshes it before every push to a full-mode
box, and `pi-bridge.service` runs it from an `ExecStartPre=+` line for lite mode. A missing token or invalid `SBX_GATE`
removes both files, so the tools fail loudly instead of using a stale credential; `SBX_GATE*` is never put in Pi's
environment.

## Workspace tools (bash, read, write, edit, glob, grep)

These run on the sandbox box in its persistent workspace, `/var/lib/sbx-work`, mounted at `/deskbox/work` only for
tools whose `tools.list` line says `work`; a `work` tool never gets `net`. Paths are relative to the workspace and
anything that resolves outside it (`..`, an absolute path, a symlink out) is an error the model sees. `bash` runs
`bash -c`, kills its process group at its timeout, cuts each stream at 30,000 characters and has **no** `keep`; the
box is reset after it when the reset switch is on. The read/glob/grep and write/edit tools carry `keep`, so a read
does not cost a reboot.

The workspace survives a reset (the reset clears RAM/processes, not the disk). `provision-sbx.sh` and
`setup-lite.sh` create it for a new image. An existing full-mode box also needs the current `/usr/local/sbin/sbx-install`
and `/opt/sbx/sbx-run`: the one-time box bootstrap installs the new `sbx-install`, `sbx-run` and `/var/lib/sbx-work`
(`sbx:sbx 0700`). After that, `sbx-push-tools` carries `tools.list`, `toollist.py`, `sbx-run` and `tools/` on every push.
The box smoke test checks the workspace.

## Adding a tool

Tools are not installable from the console. Add one by hand:

Tools live in the DeskboxTools repo, checked out here as the `tools/` submodule (`git submodule update --init` after a
clone). Work in that repo, then move the submodule pointer in Enclave.

1. Add a line to `tools/tools.list`: `name [timeout_seconds] [flags]` (timeout default 30, max 120; flags `net`, `work`,
   `keep`, see the file's header). Add `net` only if the tool needs the sandbox's network namespace; without it the tool
   runs with no network.
2. Write `tools/<name>/run` (runs on the sandbox side; reads JSON on stdin, writes JSON on stdout).
3. Write `tools/<name>/tcs.yaml` (the contract, `name: <name>-sbx`, on the trusted side). It must keep
   `allowed_side_effects.network: false` and declare the broker socket in `sandbox.bind`; `sync.py` refuses a contract that
   drops either, because a stub with no socket and no network cannot call anything.
4. Run `python3 sync.py --check`. It validates everything; the desk's `run.py` (the generic stub, sized per tool) is
   generated at deploy, never committed.
5. Deploy: `tools/tools.list` goes to `/etc/sbx/tools.list` (harness) and `/opt/sbx/tools.list` (sandbox side), and
   `tools/<name>` to `/opt/sbx/tools/<name>` (without `tcs.yaml`); in full mode `sbx-push-tools` then sends them to the
   box. Then `sudo python3 sync.py --deploy /opt/deskbox/tools && sudo chown -R deskbox:deskbox /opt/deskbox/tools` — the
   desk only serves what is in that directory, and 0700 on the run scripts is what stops anything but the desk from
   executing them.
6. Pin the stub: `agent-desk -pin /opt/deskbox/tools/<name>-sbx`, then paste `integrity.run_sha256` into
   `tools/<name>/tcs.yaml` and re-deploy. The desk checks the pin at load and again per job.

If `tcs-verify` is on `PATH`, `sync.py` runs it over every contract and fails on what it finds; it is the only thing here
that parses the schema the way the gate does, so it is what notices a keyword the validator would silently drop (a
`default:` in a property, for instance). In a checkout that cannot store the exec bit, `tcs-verify` also warns that
`run.py` is group/other-readable: the deployed copy is written 0700, which is the mode the desk actually checks.

The broker and `sbx-run` re-read their `tools.list` on each request, so list edits apply without a restart. A network
tool reaches only what the Firewall tab allows in full mode; in lite mode the sandbox is this VM, so the tool shares its namespace and can reach loopback.

### Deploying the stubs and the broker together

The stubs send `caller` and the broker refuses any request whose caller is not `<tool>-sbx`, so they are one change:
an old broker with new stubs, or new stubs with an old broker, answers 403 or 400 for every call. Deploy
`sbx-broker`, `sync.py --deploy` and the stubs from the same commit.

`caller` alone is a claim the job writes. What makes it something the job was *given* is the desk's signature: the broker
creates `/run/sbx/job.key` (0640, group `sbxdesk`, kept across a broker restart, new after every boot), `deskbox-run`
starts the desk with `-job-key /run/sbx/job.key`, and the desk then puts `DESKBOX_JOB_TS` and `DESKBOX_JOB_MAC` (an HMAC
over caller, job id, attempt and time) into each job's environment. The stub sends them; the broker recomputes the MAC,
refuses one older than 5 minutes, and refuses a second use of the same (job, attempt) (`403 bad_token`,
`403 token_replayed`, each logged `ALERT`). The key is never bound into a job. This needs the DeskBox binary built from a
commit that has `-job-key` (see `DESKBOX_COMMIT`); an older desk starts jobs without a token and every call is refused.

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

**On the core:** `sudo sbx-mode box` points the broker at `10.77.0.2`. This VM's sshd stays on: it is loopback-only and is your way in
(see Tailscale below), and the console's input chain gives the sandbox link nothing but ping. `sudo sbx-mode lite` goes back.
Then allow what the sandbox needs in the console's Firewall tab (resolver first, then host:port rules).

**On the sandbox:** `sudo sbx-node-test` checks it (default expects every outbound request to be blocked; after allowing a host on
the core, `sudo sbx-node-test --expect open <host>`). `sudo sbx-smoke-test` on the core checks the other half, including the
greet tool running through the broker into the sandbox.

On a physical sandbox box the same layout applies with a real second NIC pair or a crossover cable; set the core's `SBXC_SBX_IFACES`
(default `eth1`) to the NIC that faces it.

**Per-job reset.** The broker answers the job, then runs `SBX_RESET_CMD` while holding its one-job lock, and the next job
starts as soon as that command returns. `sbx-mode box` sets it to `sbx-reboot-and-wait` and `sbx-mode lite` clears it;
the console's Settings tab can switch the routine reset off (`/etc/sbxfw/reset-off`, logged on every job), never the
retry of a reset that failed. The queue wait (300 s) covers a reboot, and `sync.py --check` refuses a contract whose
`timeout_ms` cannot cover queue + reset retry + tool. A reset that returns early is worse than none: the broker would hand out work on a box in an
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
here, not code. `sbx-mode` sets only the shipped reboot command; the console shows it read-only. To use a host-side helper
instead, set `SBX_RESET_CMD` by hand in `/etc/sbx/broker.env` (root) and restart `sbx-broker`.

Notes: the sandbox's `/etc/resolv.conf` is immutable (`chattr -i` before changing it). Tools in the sandbox run in `bwrap` with no
network unless their `tools.list` line says `net`; a network tool shares the sandbox's network namespace and reaches only what the
core's firewall forwards for the sandbox, which is the full-mode path; in lite mode a net tool shares the core's namespace and reaches loopback without it.

## Tailscale and bridge peers (installed, off by default)

The image has `tailscaled` and `tailscale` (pinned in `image/manifest.env`) and `tailscaled.service` is enabled, but
this node is **not logged in**: no auth key or account is baked in, the same rule as the Bitwarden token. Peer
messaging is off as well: `pi-bridge.service` starts without `--peers`, so it opens no peer listener.

To join your tailnet, on the core as `sbxadmin`:

```sh
sudo tailscale up --accept-dns=false   # prints a login URL; approve this node on any device on your tailnet
```

`--accept-dns=false` keeps tailscaled from rewriting `/etc/resolv.conf` (its unit runs with `ProtectSystem=strict`);
the bridge talks to peers by tailnet IP, so MagicDNS is not needed.

**Operator SSH over the tailnet.** Forward the tailnet's port 22 to the loopback sshd, which admits `sbxadmin` by key:

```sh
sudo tailscale serve --bg --tcp 22 tcp://127.0.0.1:22    # then from your machine: ssh sbxadmin@<this node's tailnet name>
```

Not `tailscale up --ssh`: Tailscale SSH needs tailscaled to switch users, and this image's unit withholds that on purpose
(only `CAP_NET_ADMIN` and `CAP_NET_RAW`, `NoNewPrivileges`, `ProtectHome`), so it answers "operation not permitted". The
forward needs no privilege, sshd never listens off loopback, and your tailnet's access rules decide who reaches it. Add
your public key to `/home/sbxadmin/.ssh/authorized_keys` from the console first. `tailscale status` then shows the tailnet and
`sudo tailscale down` leaves it. Node identity lives in `/var/lib/tailscale`; `sudo systemctl disable --now tailscaled`
takes the agent out of boot. Nothing here is required for the single-machine setup.

To turn peer messaging on, add `--peers` to the bridge's `ExecStart` and restart it:

```sh
sudo systemctl edit --full pi-bridge     # append  --peers  to the ExecStart line
sudo systemctl restart pi-bridge
```

The peer listener binds this node's tailnet address on TCP 9761 only; the local API stays on `127.0.0.1:9760`. The
console's generated input chain includes `iifname "tailscale0" tcp dport 9761 accept`, so peers can reach the
listener; peers remain off until you add `--peers`.

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
  the core. The shipped full-mode hook is `sbx-reboot-and-wait` (a reboot, not a disk revert). The console shows the
  command read-only; `sbx-mode` owns it.

## Secrets (Bitwarden Secrets Manager)

The core fetches its LLM keys (`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `REQUESTY_API_KEY`, or any provider's `*_API_KEY`)
from Bitwarden when `pi-bridge` starts. Nothing secret is in the image or the repo. The keys stored there are the provider
list: Pi offers only models whose provider has a key, and a session picks one with `set_model` (`get_available_models` lists
them).

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
