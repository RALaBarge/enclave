# deskbox-sbx: status, tasks, decisions

Last updated 2026-10-15. Branch `gateway-firewall` carries the in-progress work; `main` is the last settled state.

## Done

- VM image pipeline (Packer + QEMU, two stages). Built and booted; `sbx-smoke-test` passes 18/18 including pi-bridge driving the
  real Pi, default-deny firewall, loopback-only listeners, greet through broker -> ssh -> bwrap.
- Loom, Ollama + `nomic-embed-text`, DeskBox, Pi 1.2.0 (release tarball, pinned baseline Bun) baked in; everything pinned in `image/manifest.env`.
- `harness/bridge` (pi-bridge): HTTP/SSE API over Pi RPC, no local login, loopback only; tailnet peer messaging with open discovery,
  X25519 first-contact pairing and signed requests. Verified between this PC and devbox.
- Apache-2.0 licence for this repo. First-install warnings now describe the strongest setup (sandbox on its own device behind the core).

## Tasks left, roughly in order

1. **Gateway firewall for sandbox traffic** (in progress on this branch). Core is the sandbox's gateway: the allowlist applies to the
   forward chain for the sandbox interface (host:port + DNS, reuse the console), masquerade out; the core's own output is open.
   Sandbox side: output only to the core address. Two-VM layout with a private link between them and a second NIC on the core.
   Smoke test changes from "core outbound blocked" to "sandbox traffic blocked unless allowlisted". Test in a throwaway netns, never in WSL's main one.
2. **Log everything to Loom from the bridge.** Loom keeps only the last user message and first assistant reply per `/log` call, so tool
   calls and thinking never get in. The bridge sees every Pi event and can post after each `agent_settled`.
3. **Web UI.** Thin client over the bridge endpoints, including a hierarchical directory manager (expand/collapse, new/rename/delete,
   pick a session's working directory). Needs bridge endpoints for listing and managing paths, confined to allowed roots (no `..`, no symlink escapes).
4. **rsync tool for Pi** (where it lives is a decision, below).
5. **Secrets** via Bitwarden Secrets Manager (`bws run`) on the PC and devbox; in the core VM, either allowlist Bitwarden or inject at boot as systemd credentials.
6. **Tailscale in the image** so bridge peers work from the core (the core is allowed egress under the new design).
7. **Bridge polish:** session persistence across restarts, file uploads.
8. **Reset hooks:** per-run reset (`SBX_RESET_CMD`), and the full-mode ones (`sbx-vm-revert`, `sbx-reboot-and-wait`) are not shipped.
9. **DeskBox licence** in `ralabarge/deskbox`, then rebuild the image from a commit that carries it.
10. **Push to GitHub** (Git Credential Manager over HTTPS; one browser approval). Nothing is pushed yet.
11. Cross-check `ralabarge/beigebox` for tools and features worth porting.
12. Not tried yet: Linux/KVM host build, VirtualBox/OVA, UEFI boot. `pi --version` prints `0.0.0`.
13. Housekeeping: large temp files in the session scratchpad (ISO cache, packer logs, installed.qcow2).

## Open decisions

| # | Decision | My recommendation |
|---|---|---|
| 1 | Gateway as a **network-level gateway** (routing + allowlist) or an **application proxy** on the core? | Network-level: any protocol, reuses the console. A proxy only controls HTTP. |
| 2 | **Where to run the two-VM setup:** QEMU on this Windows PC, or on **Proxmox** (isolated bridge with no uplink for the sandbox VM, second NIC on the core)? | Proxmox is the natural fit and removes the WHPX workarounds. Needs your OK; the image is the same. |
| 3 | **Web UI:** build our own (Svelte, small) or adopt piface / pi-web? | Our own, using piface as the architecture reference. |
| 4 | **rsync tool:** a sandbox tool (local paths only) or a harness-side Pi extension (remote hosts, with allowlist and dry-run)? | Harness-side extension; the sandbox has no route out. |
| 5 | **Loom fidelity:** fold tool calls into the logged text, or extend Loom's `/log` schema? | Extend the schema in your Loom; folding loses structure. |
| 6 | **Secrets in the core VM:** allowlist Bitwarden (simple) or boot-time injection (no egress needed)? | Allowlist, now that the core has egress. |
| 7 | **Peers on by default in the image?** | Off; one flag (`--peers`) once Tailscale is in. |
| 8 | **Lite mode:** keep as the default image, or make two-VM the main path and demote lite? | Keep lite for quick use; make two-VM the documented secure path. |
| 9 | **DeskBox licence:** Apache-2.0 to match this repo? | Yes, if you hold all the copyright. |
| 10 | **GitHub repo:** public or private, and what name? | Private until the licence and secrets handling are settled. |
| 11 | **NAS sync tool:** `robocopy /MIR` (works now) or install rsync in WSL Debian? | robocopy; it does the same job. |
