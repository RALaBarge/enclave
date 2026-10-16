# Web UI

Served by pi-bridge at `http://127.0.0.1:9760/ui/` (no login, same loopback rules as the API). No build step, no dependencies.

**`index.html` is the engine.** It owns the DOM, the network and the tab bar. Everything else is a module in `modules/`
(listed in `modules/modules.json`), and a module owns its own styles.

**A module is JSON in, JSON out**, like a DeskBox tool. It is one ES module that exports:

| | |
|---|---|
| `spec` | `{id, title, order, allow: [path prefixes it may call]}` |
| `css` | its stylesheet (shadow-DOM scoped; `--bg --fg --mut --card --bd --ac --ok --bad` are inherited from the page) |
| `update(msg) -> out` | the only entry point |

`msg` is `{type:"init"}`, `{type:"ui", name, d, values}` (a click/input/Enter on a node, with every named field's value),
`{type:"result", id, status, body}` (answer to a call), `{type:"event", stream, seq, data}` (SSE), or whatever another
module emitted. `out` is `{view?, calls?, streams?, emit?, focus?, toast?}`: a view tree, HTTP calls to make
(`{id, method, path, body?}`), SSE streams to open or close, messages for other modules, a tab to focus.
A view is `["tag", {attrs}, ...children]` with strings as text; `view.js` lists the allowed tags and attributes. Text only
reaches the page as text nodes, so nothing a model prints can inject markup.

**Nothing blocks the hot path.** Each module runs in its own Worker. The engine batches what it sends (10 ms), renders
in `requestAnimationFrame`, and does all fetches and EventSource streams itself. A module that is slow or throws
costs only its own tab. A module may only call paths under its `spec.allow`; the page CSP allows only this origin plus
hashes of the page's own two inline blocks.

New module: copy `modules/sessions.js`, add it to `modules/modules.json` and to the file list in `image/provision.sh`.

Tests: `node --test harness/ui/test/ui.test.mjs` (modules run as plain functions, no browser) and the bridge tests
(static serving, CSP, path confinement). Checked in a real browser too: create a session, open it, send a prompt, see the reply.
