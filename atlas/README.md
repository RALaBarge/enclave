# Enclave Atlas

A 3D map of the project: every file and function in Enclave, its `tools/` submodule and deskbox (at the commit
`image/manifest.env` pins), the calls between them, and the services they run as. It opens on the running system
and goes in layer by layer: service → files → a file's functions → a function's callers and callees.

Built page: `docs/atlas/index.html` (served by GitHub Pages at `/atlas/` when Pages publishes `docs/` from `main`).

Rebuild after code changes, from the repo root, with a deskbox clone beside this repo (or `DESKBOX_REPO=<path>`):

    python3 atlas/build.py

| File | What it is |
|---|---|
| `extract.py` | Walks the repos: Python by AST; JS, shell, Go and PowerShell by pattern. Emits files, symbols and calls. |
| `enrich.py` | Callers, "reached by tests", "nothing calls this", and where the 2026-11-15 security fixes landed. |
| `runtime.json` | The runtime layer, written by hand: machines, services, sockets and the flows between them. |
| `template.html`, `engine.js` | The page: panels and styles; the renderer (three.js, one instanced mesh for all nodes, one line buffer for all links) and the d3-force-3d layout. |
| `build.py` | Runs the two passes and assembles `docs/atlas/index.html`. |

Limits: calls are found statically, so calls made over HTTP, through route tables, callbacks or another language are
missing; "reached by tests" undercounts code that tests drive over HTTP. `runtime.json` has to be kept in step by hand.
