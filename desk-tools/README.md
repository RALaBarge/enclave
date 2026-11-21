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

A stub's `key` names an entry (default `openrouter`). Any OpenAI-compatible `/chat/completions` endpoint works.
The job has `network: true`, so the core's firewall decides which hosts it reaches.

Tests: `python3 -B -m unittest discover -s desk-tools/fanout`.
