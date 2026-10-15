# TODO

- [ ] **rsync tool for Pi.** Decide where it lives before writing it:
  - sbx tool (`sbx/tools/rsync`, listed in `tools.list`): moves files between the harness workspace and the sandbox workspace.
    The sandbox has no route out, so this is local-path only.
  - Pi extension on the harness side: syncs to remote hosts (devbox, nas, pve over the tailnet). It runs outside the sandbox,
    so it needs an allowlist of hosts and paths, and `--dry-run` first.
- [ ] Cross-check `ralabarge/beigebox` for tools/features worth porting (why it came up: unknown, see notes).
- [ ] Choose the DeskBox licence in `ralabarge/deskbox` (this repo is Apache-2.0) and rebuild the image from a commit that carries it.
- [ ] **Gateway firewall for sandbox traffic.** Core is the sandbox's gateway: allowlist applies to the forward chain for the
  sandbox interface (host:port + DNS, reuse the console), masquerade out; core's own output open. Sandbox side: output only to the
  core address. Two-VM QEMU layout (private net between them, second NIC on the core). Update the smoke test: "sandbox traffic
  blocked unless allowlisted" instead of "core outbound blocked". Lite mode keeps no network per tool unless a tool needs more.
- [ ] Tailscale in the VM image for bridge peer messaging (needs firewall openings and a login; bridge peers are off for now).
- [ ] **Log everything to Loom from the bridge.** Loom is passive: it stores only what a harness POSTs to `/log`, and each call
  keeps just the last user message and the first assistant reply (plus token usage), so tool calls and thinking never get in.
  The bridge sees every Pi RPC event, so it can post after each `agent_settled`. Capturing more than prompt/answer means either
  folding tool calls into the logged content or extending Loom's `/log` schema. (Loom's docs say Pi logs via a
  `hook:session_end` line; I have not verified that Pi 1.2.0 has such a hook.)
- [ ] **Web UI directory manager:** a hierarchical file tree (expand/collapse, new/rename/delete, pick a session's working
  directory). Needs bridge endpoints for listing and managing paths, confined to allowed roots (no `..`, no symlink escapes).
- [ ] Secrets: Bitwarden Secrets Manager (`bws run`) on the PC and devbox; in the VM, inject at boot as systemd credentials via
  QEMU `fw_cfg` so the image needs no token and no egress.
- [ ] Bridge: session persistence across restarts, file uploads, thin UI over the endpoints.
- [ ] Per-run reset hook (`SBX_RESET_CMD`) and full two-box mode hooks.
- [ ] Push `deskbox-sbx` to GitHub (not pushed yet).
