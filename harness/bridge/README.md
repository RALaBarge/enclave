# pi-bridge

A small HTTP/SSE API in front of `pi --mode rpc` (one Pi subprocess per session), plus optional **peer messaging**
between bridge instances on a Tailscale tailnet. No UI: a UI is just a client of these endpoints.
Python stdlib only; runs anywhere Pi does.

```
python server.py --pi-bin /usr/local/bin/pi            # API on 127.0.0.1:9760, token in ~/.pi-bridge-token
python server.py --peers                               # also enable tailnet peer messaging (off by default)
python -B -m unittest discover -s tests                # 10 tests, uses a fake Pi
```

## Local API (127.0.0.1 only, `Authorization: Bearer <token>`)

| | |
|---|---|
| `GET /health` | |
| `GET/POST /sessions`, `DELETE /sessions/{id}` | create with `{cwd, name?}` |
| `POST /sessions/{id}/command` | any Pi RPC command object (`prompt`, `steer`, `abort`, `get_messages`, `fork`, ...); returns Pi's response record |
| `POST /sessions/{id}/ui-response` | answer an extension UI request, no reply expected |
| `GET /sessions/{id}/events?since=N` | SSE; resumable with `Last-Event-ID`; ring of the last 5000 events |
| `GET /sessions/{id}/stderr` | last 200 stderr lines from Pi |

Refuses a non-loopback `--listen`, bad `Host` (DNS rebinding) and foreign `Origin` (CSRF). Reach it remotely through an SSH tunnel.

## Peer messaging (`--peers`)

Lets sessions on different machines send each other messages.

* **Discovery:** `GET /peers` runs `tailscale status --json`, probes each online node on port 9761, and reports which run a
  bridge. It also lists other Tailscale profiles (`tailscale switch --list`); only the active tailnet is reachable.
* **Send:** `POST /peers/{node}/send {text, to_session?, from_session?}`.
* **Identity:** the receiver asks the local tailscaled (`tailscale whois`) who owns the source address. Nothing in the body is
  trusted for identity. Allowed logins default to this node's own; add more with `--peer-allow`. Tagged nodes with no user
  login are refused.
* **Listener:** bound only to this machine's Tailscale IPv4, port 9761, serving only `/peer/hello` and `/peer/message`
  (64 KiB limit, 30 messages/min per peer, 500-item inbox).
* **Inbox, not injection:** messages wait in `GET /inbox`. `POST /inbox/{id}/deliver {session}` hands one to a Pi session as a
  follow-up inside a `<peer-message untrusted="true">` envelope (closing tags in the text are neutralised). `POST
  /inbox/{id}/dismiss` drops it. Auto-delivery from a given node to a given session exists (`App.auto_deliver`) but is not
  wired to any flag or endpoint yet, deliberately.

### Caveats
* A peer message is untrusted input to an agent that has tools. The envelope is a mitigation, not a boundary. Deliver
  deliberately, and keep the sandbox doing the real enforcement.
* Tested end to end on one machine (real Tailscale identity lookup, discovery over the live tailnet). Not yet exercised
  between two machines.
* Runs on the **harness** side. The sbx box has no route out and must not run this.
* The image does not ship Tailscale or this service yet.
