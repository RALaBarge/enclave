# sbx VM: setup and use

> **WARNING: this VM is not a security boundary.**
> A VM limits what a misbehaving agent or tool can do. It does not make it safe. A hypervisor or VM escape
> lands on the host. Run this only on a **disposable machine with no route to anything you care about**
> (no shared folders, no mounted disks, no LAN access to real systems, no stored credentials you need).
> Keep the firewall in default-deny. Also enforce egress at the hypervisor or network edge, so the VM cannot
> reach out even if its own firewall is misconfigured.
>
> - **Lite mode (default):** the sandbox runs inside this VM. Each tool runs in its own `bwrap` with no network.
> - **Full mode:** the sandbox runs on a separate physical box, which is the stronger option. See below.

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

## Firewall allowlist

The fresh install allows **no egress at all**. Add only what the agent needs, for example:

| Destination        | Port | Why                         |
|--------------------|------|-----------------------------|
| your LLM API host  | 443  | model calls (e.g. `openrouter.ai`) |
| your DNS resolver  | 53   | name lookup (UDP and TCP)   |

Then:

1. In the **Firewall** tab, add the entries and click **Apply**.
2. The change goes live immediately and **reverts after 60 seconds unless you confirm**. Confirm in the page
   while the rules still work. A bad rule cannot lock you out.
3. Only confirmed rules are saved to `/etc/sbxfw/ruleset.nft`, which loads at boot.

Hostnames resolve when you click Apply. CDN hosts rotate IPs, so re-apply if a service stops working.

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

## Full mode (sandbox on a separate physical box)

Full mode moves the sbx role off this VM, so an agent that escapes the sandbox still does not reach the broker's host.

1. On the sandbox box: install `/opt/sbx/sbx-run` (`sbx/sbx-run`), `/opt/sbx/tools/` and `/opt/sbx/tools.list`.
   Create an unprivileged `sbx` user. In its `authorized_keys`, force the command:
   `command="/opt/sbx/sbx-run",restrict ssh-ed25519 AAAA...`
2. Apply the two-NIC rules from the design notes on both boxes. Keep default-deny on each.
3. On this VM, set `SBX_HOST=<sandbox box address>` in `/etc/sbx/broker.env` and put the broker's private key at
   `SBX_KEY`. Pin the sandbox host key in `/etc/sbx/known_hosts`.
4. Copy tools and `tools.list` to the sandbox box by hand. The broker's key is locked to `sbx-run`.
5. Set `SBX_RESET_CMD` to a reset that restores the sandbox after each job. **The reset hooks
   (`sbx-vm-revert`, `sbx-reboot-and-wait`) are not shipped.** Write them yourself before using full mode.
6. `sudo systemctl restart sbx-broker`, then run the smoke test.

## Verify

```sh
sudo sbx-smoke-test                # full check, including the greet call through the broker
sudo sbx-smoke-test --quick  # skips the egress probe and the greet call
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
