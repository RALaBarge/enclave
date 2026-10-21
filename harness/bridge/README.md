# pi-bridge

A small HTTP/SSE API in front of `pi --mode rpc` (one Pi process per session), plus optional messaging between your own
machines over Tailscale. The web UI in `../ui` is a client of these endpoints. Python stdlib only.

```
python server.py --cwd-root /var/lib/pi-bridge-work --pi-bin /usr/local/bin/pi   # API on 127.0.0.1:9760
python server.py --cwd-root /var/lib/pi-bridge-work --peers                      # also talk to your other machines
python server.py --cwd-root /var/lib/pi-bridge-work --loom-url http://127.0.0.1:9741   # also log settled turns to Loom
python -B -m unittest discover -s tests         # uses a fake Pi
```

## Local API

Loopback only, **no login**. A web page elsewhere can't drive it: `Host` must be loopback and any `Origin` must be the
API's own. For another machine, use an SSH tunnel.

| | |
|---|---|
| `GET /health` | |
| `GET /ui/` | the web UI (`../ui`, see its README) |
| `GET/POST /sessions`, `DELETE /sessions/{id}` | create with `{cwd, name?}` |
| `POST /sessions/{id}/command` | a Pi RPC command from the allowlist `COMMANDS` in `server.py` (`prompt`, `steer`, `abort`, `get_messages`, `fork`, ...; never `bash`); returns Pi's response |
| `POST /sessions/{id}/ui-response` | answer an extension UI request |
| `GET /sessions/{id}/events?since=N` | SSE, resumable via `Last-Event-ID` (last 5000 events kept) |
| `GET /sessions/{id}/stderr` | last 200 stderr lines |
| `POST /sessions/{id}/files?path=rel` | raw body, `Content-Length` required, 8 MiB cap; returns `{path, bytes}` |
| `GET /sessions/{id}/files?path=rel` | bytes from under the session's `cwd`, 8 MiB cap |

## Session cwd roots (`--cwd-root`)

`--cwd-root` is required and repeatable. Every session cwd is resolved with `realpath` and must be one of the roots or
below one; the roots themselves are resolved once at startup and compared resolved everywhere after that. The bridge
refuses to start without at least one root: it takes the cwd from its caller, so it is the component that must not
trust it.

Each root is also checked at startup. The bridge refuses a root that is `/`, that lies inside a protected path, or
that contains one, and names the root and the rule that failed. The bridge's home, `/var/lib/pi-bridge`
(its state and whatever Pi keeps under `$HOME`), is protected as a whole; the root the shipped unit passes,
`/var/lib/pi-bridge-work`, is a sibling directory, not a subdirectory, so no exception is needed.

`POST /sessions` with a cwd outside every root answers 403 with a message naming the roots and the flag, e.g.
`/home/me/notes is outside the bridge's allowed roots (/var/lib/pi-bridge-work); add --cwd-root if this location is
intended`. The UI shows that message. The existing protected-path checks stay as the second line of defence.

A restored session is rechecked against the roots that are in force now. A record whose cwd no longer qualifies is
not dropped: it stays in `GET /sessions` as `state: "refused"` with a `reason`, its commands answer 502, and its file
endpoints answer 403. Adding the missing `--cwd-root` and restarting re-evaluates the record; `DELETE /sessions/{id}`
removes it. The break is deliberate: a session created under a wider or implicit rule cannot be created or restored
under a narrower one.

## Sessions across restarts

`--state-dir` holds `sessions.json`, one small record per session: `id`, `cwd`, `name`, `pid`, `created_at`,
`state_dir`, `state` (`alive`, `dead`, or `refused` with a `reason`), `exit_code`, and — while the session is alive
or refused — the paths of its FIFO RPC channel. The file is rewritten atomically (temporary file in the same
directory, then `os.replace`) when a session is created, exits or is deleted.

On start the registry rebuilds from that file. A record whose cwd is outside the current `--cwd-root` list comes back
`refused` with its reason (see above) and is not reattached. For the rest, a Pi process that is still alive and
answers a `get_state` round trip on its FIFOs is reattached: same process, same channel, no new Pi. If the process is
gone, or the channel cannot be opened or does not answer, the session stays in the list as `dead` with its last known
exit code, and commands to it answer 502. `DELETE /sessions/{id}` stops the process, removes its FIFOs and removes
the record, so `GET /sessions` shows sessions from the previous run instead of forgetting them.

The FIFO channel is what makes this possible with Pi's stdin/stdout RPC mode: the child keeps a read/write end
of each FIFO, so it does not see EOF when the bridge process dies. Three consequences:

* POSIX only. On Windows the bridge uses anonymous pipes, a restart cannot reattach, and old records come back
  `dead`.
* `stop` signals a session (SIGTERM, then SIGKILL) instead of closing its stdin, because closing the bridge's
  end is not EOF for a child that holds the other end.
* Events the Pi wrote while no bridge was reading are consumed while checking that the channel works; the
  event ring lived in the old bridge process anyway, so a reattached session starts with an empty one.

A restart of the shipped systemd unit is not the case this helps: the default `KillMode=control-group` stops
the Pi processes along with the bridge, so those sessions come back `dead`. Reattachment covers the bridge
process dying while its children keep running (a crash under a supervisor that does not kill the group).

## Files

Both routes are scoped to the session's `cwd`; `path` is relative and either separator is accepted.

* `POST /sessions/{id}/files?path=rel` takes the raw body. `Content-Length` is required (411 without it) and a
  body over 8 MiB is refused before reading (413). Missing parent directories are created. It returns
  `201 {"path": "notes/a.txt", "bytes": n}` with the normalised relative path. The write is not atomic: a
  connection that ends early leaves the partial file.
* `GET /sessions/{id}/files?path=rel` returns the bytes as `application/octet-stream`; over 8 MiB is 413, a
  missing file 404, a directory 400.

Refused with 403: absolute paths (`/x`, `C:\x`, `\\server\share`), any `..` component, and any path whose
`realpath` falls outside the session's real `cwd` — so neither a symlinked directory nor a symlinked file can
be used to read or write outside it. The containment check and the open are separate system calls, so a
process that can rewrite the session's filesystem during a request could still win that race. The bridge is
loopback-only; this is not a sandbox.

Host and Origin checks apply to both routes like every other local route.

## Loom (`--loom-url`)

Off by default. With `--loom-url http://127.0.0.1:9741`, each settled turn is POSTed once to Loom's
`POST /log`; `--loom-timeout` (default 5 s) bounds each attempt. A per-session recorder reads the same event ring
the SSE relay serves, so no SSE client has to be connected for a turn to be logged.

The body is exactly the shape `/log` accepts:

```json
{
  "request": {"messages": [{"role": "user", "content": "read the notes"}]},
  "response": {"choices": [{"message": {"role": "assistant",
    "content": "Looking at the notes.\ntool: read {\"path\":\"notes.txt\"}\ntool result: read: hello\nunknown event pi_future_event: {\"type\":\"pi_future_event\",\"detail\":{\"n\":1}}"}}]},
  "metadata": {"conversation_id": "0a1b2c3d4e5f", "provider": "pi", "session_name": "work",
               "cwd": "/home/sbx/work", "turn_index": 1}
}
```

* **The user message** is the last `prompt`/`steer` text the bridge saw for that session (or a `user` message
  replayed in Pi's events), cut at 16,000 characters. Only that one message is sent; `/log` keeps just the last
  user entry anyway.
* **The assistant content is the whole turn, in one message.** Loom's `/log` stores exactly two texts: the last
  user message and the first assistant `content`; it has no field for tool calls. The recorder therefore renders
  the transcript as assistant text first, then one line per `tool_execution_start`/`tool_execution_end`
  (bounded argument and result summaries), then one line per event type this bridge does not know yet
  (`unknown event <type>: {...}`, bounded JSON). Keeping it all in one assistant message is deliberate: sending
  tool and unknown-event lines as separate messages would make `/log` store turns that never happened.
* **Bounds.** Tool argument/result summaries and unknown-event lines are cut at 500–600 characters; the
  transcript at 120,000.
* **Redaction.** Before posting, both texts go through `../redact/output_redactor.py` (secret patterns, encoded
  variants and the high-entropy sweep; its PII patterns are left off so ordinary names and addresses still reach
  memory). If the redactor raises, the turn is not sent — failing closed beats storing an uninspected secret.
  It is loaded from `../redact` when the bridge runs from the repo; an install that copies only `server.py` must
  copy that module beside it (or into `redact/`), otherwise `--loom-url` warns at startup and sends nothing.
* **Best effort.** One attempt per turn, no retries. A refusal or timeout never touches the session or its SSE
  stream; the first failure prints one line to stderr and later failures for that session stay silent. Loom is
  loopback-only and `/log` has no authentication, so keep `--loom-url` pointed at localhost.

## Peers (`--peers`)

* **Discovery is open.** `GET /peers` lists your online tailnet nodes and which ones answer `GET /peer/hello` (that reply
  is only app, version, instance id, name).
* **First contact negotiates a key.** `POST /peers/{node}/send {text, to_session?}` pairs automatically the first time
  (X25519 + HKDF, confirmed by a tag), no step for you. Every later request is HMAC-signed with a 60 s window and replay
  cache. Keys live in `~/.pi-bridge/peers.json`; `DELETE /pairings/{instance}` forgets one. If a peer loses its state, one
  send fails and the next re-pairs.
* **Who may pair:** only a tailnet address that `tailscale whois` says belongs to your own login. Everything else is
  refused. Signing, not Tailscale, decides who may send.
* **Messages wait in `GET /inbox`.** `POST /inbox/{id}/deliver {session}` hands one to a Pi session inside an
  `untrusted="true"` envelope (closing tags in the text are neutralised); `.../dismiss` drops it. Nothing reaches an agent
  on its own.
* The listener binds to this machine's Tailscale address only (port 9761).

### Caveats
* The key exchange is trust-on-first-use. On your own tailnet that is backed by WireGuard and the whois check; off a tailnet
  you would be trusting the network.
* The envelope is a mitigation against prompt injection, not a boundary. The sandbox still does the real enforcement.
* Run on the harness side only. The sbx box has no route out and must not run this.
* Verified on one machine (real pairing and signed messages through the live tailnet address). Not yet between two machines.
* The VM image does not ship Tailscale or this service yet.
