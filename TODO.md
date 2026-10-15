# Enclave: status, tasks, decisions

Last updated 2026-10-15. `main` is the last settled state; work happens on short-lived branches.

## Done

- VM image pipeline (Packer + QEMU, two stages). Built and booted; `sbx-smoke-test` passes 18/18 including pi-bridge driving the
  real Pi, default-deny firewall, loopback-only listeners, greet through broker -> ssh -> bwrap.
- Loom, Ollama + `nomic-embed-text`, DeskBox, Pi 1.2.0 (release tarball, pinned baseline Bun) baked in; everything pinned in `image/manifest.env`.
- `harness/bridge` (pi-bridge): HTTP/SSE API over Pi RPC, no local login, loopback only; tailnet peer messaging with open discovery,
  X25519 first-contact pairing and signed requests. Verified between this PC and devbox.
- Console firewall (merged): session and build profiles, allow and block lists (block wins), instant apply, build timer that falls back to session, UI. Verified in a real browser and on two VMs (instant apply, block beats allow, build profile with timer, switch back).
- Session lockdown (merged): the bridge makes the console switch to the `session` firewall profile before any session starts, with a tighten-only token (it can never loosen, and fails closed). A build window stays a human action. Verified on the VMs (core smoke 21/21).
- Project renamed Enclave (README, banners, console title; code identifiers keep `sbx`).
- MIT licence for this repo (was Apache-2.0 until Oct 15). First-install warnings now describe the strongest setup (sandbox on its own device behind the core).

## Tasks left, roughly in order

1. ~~Gateway firewall for sandbox traffic~~ **Done** on branch `gateway-firewall`: forward-chain allowlist, DNS redirect, masquerade, core output open;
   lean sandbox image; two-VM launcher; verified closed -> open -> closed on two real VMs (core 20/20, sandbox 16/16). Follow-ups:
   - **Tools that need the network:** sandbox tools run in `bwrap` with no network. A per-tool flag in `tools.list` (and `bwrap --share-net`)
     would let a tool use the gateway path. Touches `toollist.py` (3 copies), `sbx-run`, the contract and the console Tools tab.
   - Run the two-VM layout on Proxmox later (isolated bridge for the sandbox VM). For now: this PC, QEMU.
   - Independent security review of the gateway design (a larger model or a human).
2. **Log everything to Loom from the bridge.** Loom keeps only the last user message and first assistant reply per `/log` call, so tool
   calls and thinking never get in. The bridge sees every Pi event and can post after each `agent_settled`.
3a. **Request inspector + fork ("shadow agent") in the UI.** Under each assistant reply bubble, a control that opens the exact HTTP request
   that produced it (method, URL, params, system prompt, the full message list, tool definitions) in an editor. A **Fork** button in
   that panel forks the conversation at that step and re-runs it using the edited request, so you can change a prompt, a message or a
   parameter and compare the new branch against the original side by side.
   - *Capture:* Pi's RPC does not expose the raw provider request. Likely route: the bridge runs a small local recording proxy and Pi's
     provider base URL points at it, so every request/response pair is stored (this is also the raw feed for the 98% Loom capture).
     Match each request to its reply by order and a per-turn id. Redact `Authorization` and other secret headers in anything the UI
     sees; the real key stays server-side and is added when a request is replayed.
   - *Fork:* Pi RPC already has `fork {entryId}`, `clone`, `get_fork_messages` and `get_tree`, so the branch itself is cheap.
   - *Re-run:* **through Pi** (decided 2026-10-15). Fork the session at that entry with Pi's `fork`, apply the edit to the forked
     context, and run it through Pi, so tool calls execute again in the sandbox and the new branch is a real session you can keep
     working in. The raw-request recorder stays useful for the inspector view and for Loom.
   - Conversation tree view already planned for the UI doubles as the place to browse forks.
3. **Web UI.** Thin client over the bridge endpoints, including a hierarchical directory manager (expand/collapse, new/rename/delete,
   pick a session's working directory). Needs bridge endpoints for listing and managing paths, confined to allowed roots (no `..`, no symlink escapes).
4. **rsync tool for Pi** (where it lives is a decision, below).
5. **Secrets** via Bitwarden Secrets Manager (`bws run`). DONE for the core VM (Oct 15): `bws` 2.1.0 pinned, `image/sbx-fetch-secrets.sh` runs as root before pi-bridge and writes the keys to a RAM-only env file, token (root-only) placed by hand (SETUP.md). Image not yet rebuilt with it; PC and devbox not wired.
6. **Tailscale in the image** so bridge peers work from the core (the core is allowed egress under the new design).
7. **Bridge polish:** session persistence across restarts, file uploads.
8. **Reset hooks:** per-run reset (`SBX_RESET_CMD`), and the full-mode ones (`sbx-vm-revert`, `sbx-reboot-and-wait`) are not shipped.
9. **DeskBox licence** in `ralabarge/deskbox`, then rebuild the image from a commit that carries it.
10. **Push to GitHub** (Git Credential Manager over HTTPS; one browser approval). Nothing is pushed yet.
10a. **memgate** (spec: `Downloads\MEMGATE_SPEC.md`, written in a web session): a two-stage memory gate on TypeSafe Jev whose Part A changes Loom
    (ordered append-only messages with `seq`, curated memories with history, `/health` features). Not started. It touches the same Loom schema
    as the planned `events`/`blobs` extension, so plan both together before changing Loom.
11. Cross-check `ralabarge/beigebox` for tools and features worth porting.
12. Not tried yet: Linux/KVM host build, VirtualBox/OVA, UEFI boot. `pi --version` prints `0.0.0`.
13. Housekeeping: large temp files in the session scratchpad (ISO cache, packer logs, installed.qcow2).

## Decisions

Made (2026-10-15):

| Decision | Answer |
|---|---|
| Gateway type | Network-level gateway (routing + allowlist), not an application proxy |
| Where to run two VMs | This Windows PC, QEMU (Proxmox later) |
| Web UI | Build our own (Svelte-ish, small), piface as the architecture reference |
| rsync tool | Yes; harness-side Pi extension recommended (remote hosts, allowlist, dry-run first) |
| Loom format | Extend it: append-only `events` + content-addressed `blobs`, `POST /events`; store raw (the 98% version), embed only the useful subset, redact secrets first |
| Secrets | Bitwarden Secrets Manager (`bws run`); the core has egress so allowlist Bitwarden there |
| Peers | Keep, off by default (stdlib, ~350 lines); drop only if Tailscale goes away |
| Lite mode | Tabled |
| DeskBox licence | MIT (decided Oct 15; DeskBox relicensed in `e6fd82f`, pin updated) |
| NAS sync | `robocopy /MIR`; no rsync install |
| Request inspector re-run | Through Pi: fork the session at the step and run the edited request in the fork |

Still open:

| # | Decision | My recommendation |
|---|---|---|
| 10 | **GitHub repo:** public or private, and what name? | Private until the DeskBox licence and secrets handling are settled |

## From the 2026-10-15 independent review (see repos\REVIEW-2026-10-15.md; report was in OneDrive\Documents\deepseek-harness)

Fixed: F-01 lockdown now rolls back a pending confirm instead of blocking every session; F-05 the Bitwarden token is root-only and a root
ExecStartPre (`image/sbx-fetch-secrets.sh`) writes the keys to a RAM-only env file; F-06 bridge mints its own RPC ids; F-09 IPv6 Host parsing;
F-10 oversized sandbox requests get their own code; F-12 DeskBox pinned to the MIT commit and binaries rebuilt; Loom pinned to the commit with
`message_id` provenance; licence residue in other repos.
Declined: F-07 (require `Origin` on POST/DELETE): any local process can send any `Origin`, so it adds nothing against local clients, and browsers are
already refused by the existing mismatch check.
Added: `harness/redact/output_redactor.py` (stdlib secret/PII redactor with a Bitwarden-token pattern, from the old ai-stack). Not wired in yet: it is
the redaction step for the Loom events/blobs logging task.
Verified after the fixes (Oct 15): both images rebuilt; core smoke test 22/22, sandbox node test 16/16, kernel lab 28/28. NAS snapshot refreshed (hashes match).
Still to do: put the Bitwarden token on the core and confirm a real fetch (it has only run against a fake `bws`).
