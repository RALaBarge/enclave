# Enclave: status, open work, decisions

Last updated 2026-11-24. `main` is the settled state; work happens on short-lived branches and is merged back. Other docs
refer to open items here by their bold name, not their number.

## Where it stands

Core and sandbox run on two machines joined by a private cable (full mode): the core installed with `image/provision.sh`,
Pi built from pinned source and run under Node (`image/build-pi.sh`), the sandbox with `image/provision-sbx.sh` and the
bootstrap the core serves. `sbx-smoke-test` passes 32/32 on the core and `sbx-node-test` passes on the sandbox. A real model
prompt calls sandbox tools end to end, including `page` and `fetch` through the browserbox gate. The operator reaches the web UI
and the console through an SSH tunnel as `sbxadmin`. The QEMU VM path (`enclave.cmd`) still exists but has not been rebuilt since
October.

## Open, roughly in order

1. **Sandbox reset.** `SBX_RESET_CMD` is empty on the running install (a reboot per job is too slow), so jobs reuse the sandbox as
   the last job left it; each job still gets a fresh `bwrap`, and `/var/lib/sbx-work` persists by design. A reboot clears RAM and
   tmpfs, not disk. The real fix is booting the sandbox from the core (PXE, read-only root). Decide.
2. **Pin the stubs.** No contract carries `integrity.run_sha256`. Run `agent-desk -pin` per tool, put the hash in DeskboxTools'
   contracts, redeploy. The console's Tools tab shows "not pinned" until then.
3. **pi-bridge restart ends every Pi session.** `server.py` stops them all on SIGTERM; `KillMode=process` only keeps them alive
   across a crash. Either reattach on a clean restart too, or say in the UI that a restart ends sessions. Decide.
4. **page/fetch errors.** A URL the gate refuses (not on its allowlist) reaches the agent as `sbx stub failed: RuntimeError`.
   Pass the gate's reason through.
5. **Deploy what is merged but not installed:** `fanout` (needs `keys.json` on the core, see `desk-tools/README.md`) and the
   inline-spill desk extension (`docs/INLINE-SPILL.md`).
6. **UI against real Pi:** chat is checked against real events (2026-11-24); Tree and Inspector are not. Directories: add
   `GET /dirs` so folders without sessions appear.
7. **Smoke test gaps (review J):** nothing proves a `-sbx` job itself has no network, and `check_listeners` looks at TCP only.
8. **Loom:** `events`/`blobs` tables and `POST /events` (decided Oct 15), then memgate (spec lost; settle window scoring,
   the sensitive flag versus auto-write, and the calibration harness first).
9. **Request inspector raw view:** a recording proxy in the bridge that Pi's provider base URL points at, with `Authorization`
   redacted; also the raw feed for Loom and the fixtures for the Go runner.
10. **Go runner** (`docs/PI-FEATURES.md` build order), then retire Pi.
11. **Requesty:** needs a `models.json` provider entry with model IDs.
12. **VM images:** rebuild before using the VM path again (they predate the November changes); builds need a Windows
    checkout with the Packer cache restored.
13. **Independent review** of the UI engine's sandboxing (Workers, view whitelist, CSP) and of the console's new Tools/Activity
    endpoints.
14. **More UI modules** (researched Oct 16): turn timeline; egress ledger (allow once/always/never from the firewall log);
    sandbox diff per turn; replay a turn against another model or profile; memory inspector; approval gate owned by the desk;
    cost/latency ledger; turn bundle export.
15. **Tests that cannot fail (review O):** a few tests assert the implementation's own constant or a cwd the test chose; see the
    2026-10-18 review report.

## Done (milestones)

- **Oct 15-23:** VM image pipeline (Packer + QEMU); pi-bridge; console firewall with session/build profiles; session lockdown
  through the gate's tighten-only token; desk tools track (DeskBox `-strict`, broker socket, signed job tokens, tools adapter);
  web UI engine; Bitwarden Secrets Manager; Tailscale and bridge peers (off by default); tools moved to DeskboxTools; first
  full-mode cutover with the sandbox on its own box.
- **Nov 14-15:** core moved to bare metal; Pi under Node (the sandbox-class CPUs lack SSE4.2, so Pi's Bun binary cannot run);
  sandbox `install` verb and the core-served bootstrap; security review fixes (secrets in `/run`, broker locks the firewall
  before `net` jobs, forwarding only after nftables loads, and more); Atlas.
- **Nov 21:** `fanout` desk tool. **Nov 24:** operator UI (SSH tunnel for `sbxadmin`; chat with thinking and a tool-call column;
  console mode from `SBX_HOST`, Tools code view, Activity tab); page/fetch back on; branches merged and cleaned.

Review findings from 2026-10-18 closed since: A (stub timeout sized from the contract), B/C (rsync tool removed), D, E, G
(bind destinations under `/deskbox/tool` refused, pinned DeskBox), H (separate gate groups), I (smoke test reads the bridge's
real groups), K, L, M, N, P.

## Decisions

| Decision | Answer |
|---|---|
| Gateway type | Network-level gateway (routing + allowlist), not an application proxy |
| Where it runs | Two machines on a private cable (strongest); QEMU VMs on Windows as the alternative; lite mode for convenience only |
| Web UI | Our own, small, no build step |
| Firewall console as a UI module | Dropped (branch deleted 2026-11-24): the bridge would have had to proxy the console with its token, letting Pi edit its own firewall |
| Loom format | Append-only `events` + content-addressed `blobs`, `POST /events`; store raw, embed only the useful subset, redact secrets first |
| Secrets | Bitwarden Secrets Manager; read-only access token on the core; model keys in `/run`; the gate token only in the page/fetch tool directories |
| Peers | Keep, off by default |
| MCP | Declined (2026-10-18): it does not move the enforcement floor (contract + bwrap + the machine split) and makes the tool surface runtime prompt text instead of pinned files. Harness adapters generate from the desk's `GET /tools` |
| Where tools live | DeskboxTools, checked out as `tools/`; the desk is the only executor; core-only tools that need a secret go in `desk-tools/` |
| Console access to the desk | Reads contracts (0640, group `sbxconsole`) and the desk's `/jobs`; never the stubs (0700: reading one is enough to run it past its contract) |
| Repository | Public (`RALaBarge/enclave`); docs and the Atlas keep LAN and tailnet addresses out |
| Licence | MIT, as is DeskBox |
