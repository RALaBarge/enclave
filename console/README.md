# sbx-console

Local control panel for the deskbox sandbox bridge: **Firewall** (nftables, default-deny in and out),
**Tools** (health of what's in `tools.list`, enable/disable, timeouts) and **Settings** (broker env, lite/full mode).
Stdlib Python, one static page, no framework. Binds `127.0.0.1:8787`; every API call needs the bearer token.

Tools are deliberately **not** installable from the UI. Adding one means writing sandbox code, a contract and a
pinned stub by hand (`sync.py`), then re-pinning in agent-desk. The Tools tab shows the stub hash to pin.

## Run it on a disposable computer

A VM limits the damage a misbehaving agent or tool can do. It does not make it safe: a VM escape lands on the host.
For full security run the whole thing on its own machine with no route to anything you care about, and keep the
firewall in default-deny. The UI shows this warning on first launch.

## Modes

- **Lite (default):** sandbox runs in this VM; each tool in its own `bwrap` (no network, fresh tmpfs per job).
  Set `SBX_HOST=127.0.0.1` and run `sshd` locally with the forced-command key (see the main setup).
- **Full:** `SBX_HOST` points at a separate physical box. Add the two-NIC rules from the design notes there.
  The broker's SSH key is locked to `sbx-run`, so tools and `tools.list` are copied to that box by hand.

## Install (Debian 13)

```sh
sudo apt install nftables python3 bubblewrap openssh-server
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin sbxconsole
sudo install -d -o sbxconsole -g sbxconsole /etc/sbxfw
sudo install -d /opt/sbx-console/common
sudo install -m 644 console/sbx_console.py console/ui.html /opt/sbx-console/
sudo install -m 644 common/toollist.py /opt/sbx-console/common/
sudo install -m 644 console/nftables.conf /etc/nftables.conf      # Debian's stock path
sudo install -m 644 console/sbx-console.service /etc/systemd/system/
sudo -u sbxconsole SBXC_ETC=/etc/sbxfw python3 /opt/sbx-console/sbx_console.py --init   # token + default-deny ruleset
sudo chgrp sbxconsole /etc/sbx /etc/sbx/*  2>/dev/null; sudo chmod g+w /etc/sbx /etc/sbx/tools.list /etc/sbx/broker.env
sudo systemctl enable --now nftables sbx-console
sudo cat /etc/sbxfw/token      # paste into the page at http://127.0.0.1:8787
```

Fresh install = **no egress at all**. Add what the agent needs, e.g. `openrouter.ai 443` and a DNS resolver, then
Apply. A change goes live immediately and **reverts after 60 s unless you confirm**, so a bad rule can't lock you out.
Only confirmed rules are saved to `/etc/sbxfw/ruleset.nft`, which `nftables.service` loads at boot.

## Known limits

- Hostnames are resolved when you apply. CDN hosts rotate IPs; re-apply if a service stops working.
- The revert timer lives in the console process. If it crashes mid-window, the unconfirmed rules stay until reboot.
- If the VM has no `systemd-journal` access the blocked-attempts list is empty (the unit adds the group).
- Reset hooks (`sbx-vm-revert`, `sbx-reboot-and-wait`) are not shipped; lite mode doesn't need one.

## Tests

`python3 console/test_console.py` (no root). Real-nft behaviour (apply, revert, confirm, cancel, boot-file check) was
verified in a throwaway network namespace under WSL2 Debian.
