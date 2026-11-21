# desk-tools: core-side DeskBox tools

Plain DeskBox tools that run on the trusted core, one job at a time, under the desk. They are not sandbox stubs and do not
go through the broker, so they are not in `tools/tools.list`. `python3 sync.py --check` validates them and
`sync.py --deploy DIR` installs each as `DIR/<name>/` (run.py 0700, tcs.yaml 0600). Re-pin after a change, as for the stubs.

Use this for a tool that needs a secret the sandbox must never hold.

## fanout

JSON in, JSON out: ask N models the same question in parallel, then one judge (`anthropic/claude-fable-5.2`) picks the best
answer, scores each, and writes a synthesis. The judge sees the answers shuffled and anonymous. With no `stubs` it asks the
default eight OpenRouter models listed in `run.py`.

```json
{"prompt": "Explain RAID1 rebuilds in two sentences."}
{"prompt": "...", "stubs": [{"name": "a", "model": "google/gemini-3.8-flash"}, {"name": "b", "model": "x/y", "key": "other"}],
 "judge": {"model": "anthropic/claude-fable-5.2"}, "max_tokens": 600, "timeout_s": 60}
```

Every stub gets the same request body except `model`. Providers differ only in base URL and headers, and those come from
`keys.json` in the deployed tool directory, never from the input (a caller must not be able to send a key somewhere else).
Write it on the core as the desk account, mode 0600, not from git:

```json
{"openrouter": {"base_url": "https://openrouter.ai/api/v1", "headers": {"Authorization": "Bearer <key>"}},
 "other":      {"base_url": "https://example.com/v1",        "headers": {"x-api-key": "<key>"}}}
```

### The judge: `judge.backend`

- `api` (default): an OpenAI-compatible endpoint from `keys.json`, model `anthropic/claude-fable-5.2` on OpenRouter.
- `cli`: runs Claude Code headless (`claude -p --model claude-fable-5-2 --tools ""`), so the judging bills to the Claude
  Code login on the core, not an API key. Set it up once on the core as the desk account: install Claude Code, run
  `claude` and log in, and make the binary and its home reachable in the job (the desk binds only `/usr`, `/bin`, `/lib`,
  so install it under `/usr`, or add a `sandbox.bind` for it and the home directory). If it lives elsewhere, add
  `"claude_cli": {"path": "/opt/claude/bin/claude", "env": {"HOME": "/opt/claude/home"}}` to `keys.json`. A missing or
  logged-out CLI is reported in `judge.error`; the answers still come back. Not checked against the desk's bwrap job.
- `none`: no judge, just the answers, for a caller (such as a Claude Code session) that judges them itself.

A stub's `key` names an entry (default `openrouter`). Any OpenAI-compatible `/chat/completions` endpoint works.
The job has `network: true`, so the core's firewall decides which hosts it reaches.

Tests: `python3 -B -m unittest discover -s desk-tools/fanout`.
