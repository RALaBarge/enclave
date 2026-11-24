# desk-tools: core-side DeskBox tools

Plain DeskBox tools that run on the trusted core, one job at a time, under the desk. They are not sandbox stubs and do not
go through the broker, so they are not in `tools/tools.list`. `python3 sync.py --check` validates them and
`sync.py --deploy DIR` installs each as `DIR/<name>/` (run.py 0700, tcs.yaml 0600). Re-pin after a change, as for the stubs.

Use this for a tool that needs a secret the sandbox must never hold, or a core service it must never reach.

## memq

The agent's way to memory, through Ryan's review queue in Loom (127.0.0.1:9741, fixed in `run.py`). `{"op": "push", "text",
"events"?, "note"?}` queues a candidate (`by: agent`); `list` shows the queue; `pop` moves every entry Ryan accepted into memory;
`search` looks up accepted memories. It cannot accept anything: only the console's Memory tab holds Loom's review token. Its
own token is `memq.token` beside the deployed tool (deskbox, 0600), a copy of `/etc/loom/memq.token` that `provision.sh`
writes. The same file is Ryan's command on the core (`/usr/local/bin/memq push|list|pop|search`, `by: ryan`, group `loomq`).

Tests: `python3 -B -m unittest discover -s desk-tools/memq`.

## fanout

JSON in, JSON out: ask N models the same question in parallel, then one model (the synthesizer, `anthropic/claude-fable-5.2`)
reads all the answers by name and returns one combined answer, what the models agree on, where they differ and who it thinks
is right, and its own two cents. It does not rank or score. `answers` always carries every model's full reply, in the order
asked, so you can compare them yourself. Reasoning is switched off for every model unless the input sets `thinking` to true (a model that refuses to be switched off is
retried at `effort: minimal`),
and each model is told the 16,000-character answer cap (longer replies are cut there). With no `stubs` it asks the default
eight OpenRouter models listed in `run.py`.

```json
{"prompt": "Explain RAID1 rebuilds in two sentences."}
{"prompt": "...", "stubs": [{"name": "a", "model": "google/gemini-3.8-flash"}, {"name": "b", "model": "x/y", "key": "other"}],
 "synthesizer": {"model": "anthropic/claude-fable-5.2"}, "max_tokens": 600, "timeout_s": 60}
```

Every stub gets the same request body except `model`. Providers differ only in base URL and headers, and those come from
`keys.json` in the deployed tool directory, never from the input (a caller must not be able to send a key somewhere else).
Write it on the core as the desk account, mode 0600, not from git:

```json
{"openrouter": {"base_url": "https://openrouter.ai/api/v1", "headers": {"Authorization": "Bearer <key>"}},
 "other":      {"base_url": "https://example.com/v1",        "headers": {"x-api-key": "<key>"}}}
```

### The synthesizer: `synthesizer.backend`

- `api` (default): an OpenAI-compatible endpoint from `keys.json`, model `anthropic/claude-fable-5.2` on OpenRouter.
- `cli`: runs Claude Code headless (`claude -p --model claude-fable-5-2 --tools ""`), so the synthesis bills to the Claude
  Code subscription, not an API key. The image installs the pinned binary at `/usr/bin/claude` (`CLAUDE_CODE_VERSION` in
  `image/manifest.env`; `/usr` is bound into every desk job, `/usr/local` is not) and bakes in no login. To authenticate,
  run `claude setup-token` on any machine where you are logged in, and put the token in the deployed `keys.json`:
  `"claude_cli": {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "<token>"}}`. A token rather than a login because the job's
  filesystem is read-only apart from a tmpfs `/tmp`, so nothing has to persist or refresh on disk. Use `"path"` in the same
  entry if the binary lives elsewhere. A missing binary or a bad token is reported in `synthesizer.error`; the answers still
  come back. Not checked inside the desk's bwrap job or a rebuilt image.
- `none`: no synthesis, just the answers, for a caller (such as a Claude Code session) that combines them itself.

A stub's `key` names an entry (default `openrouter`). Any OpenAI-compatible `/chat/completions` endpoint works.
The job has `network: true`, so the core's firewall decides which hosts it reaches.

Tests: `python3 -B -m unittest discover -s desk-tools/fanout`.
