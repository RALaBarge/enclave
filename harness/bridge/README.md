# pi-bridge

A small HTTP/SSE API in front of `pi --mode rpc` (one Pi process per session), plus optional messaging between your own
machines over Tailscale. No UI: a UI is a client of these endpoints. Python stdlib only.

```
python server.py --pi-bin /usr/local/bin/pi     # API on 127.0.0.1:9760
python server.py --peers                        # also talk to your other machines over the tailnet
python -B -m unittest discover -s tests         # 14 tests, uses a fake Pi
```

## Local API

Loopback only, **no login**. A web page elsewhere can't drive it: `Host` must be loopback and any `Origin` must be the
API's own. For another machine, use an SSH tunnel.

| | |
|---|---|
| `GET /health` | |
| `GET/POST /sessions`, `DELETE /sessions/{id}` | create with `{cwd, name?}` |
| `POST /sessions/{id}/command` | any Pi RPC command (`prompt`, `steer`, `abort`, `get_messages`, `fork`, ...); returns Pi's response |
| `POST /sessions/{id}/ui-response` | answer an extension UI request |
| `GET /sessions/{id}/events?since=N` | SSE, resumable via `Last-Event-ID` (last 5000 events kept) |
| `GET /sessions/{id}/stderr` | last 200 stderr lines |

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
