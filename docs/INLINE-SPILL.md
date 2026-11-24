# Inline & Spill

Written 2026-11-02. The policy that decides how much of a desk tool result reaches the model, and what it keeps as a
way back. Code: `harness/toolsadapter/inline.py` (reference), `harness/bridge/desk-extension.js` (the copy Pi loads).

## The problem it solves

The desk's tools already bound themselves:

| Tool | Its own bound |
|---|---|
| `read` | 250,000 chars, 2,000 lines per call, 2,000 chars per line; reports `total_lines` |
| `grep` | 500 matches, 5,000 chars per match; sets `truncated` |
| `bash` | stdout and stderr cut at 30,000 chars each; reports `exit_code`, `timed_out` |

Those are answers to "how much may a tool return". Nothing answered "how much of that belongs in the next request",
and the extension turned every result into a string and put all of it in the context. One `read` of a large file
injected a quarter of a megabyte and diluted every instruction around it — the failure the design article calls
wasting the context window, which is wasting the model's intelligence.

`inline.py` is that second answer, at the one place where the full result still exists and the model does not yet:
the adapter boundary. It applies to Pi today and to the in-tree loop (`docs/PI-FEATURES.md`, option 3) later, because
neither the bridge nor the broker sees the result before the model does.

## The two operations

- **Inline** — a result at or under `MAX_CHARS_DEFAULT` (16,000 chars, roughly 4k tokens) is passed through
  untouched, byte for byte. Truncation is meant to be rare, so the common case costs nothing.
- **Spill** — past the budget, large string leaves are cut to a per-field cap with a marker, and long lists keep
  fewer items with one marker row. The full content stays where it already is.

Two dials, in the order that loses least: shorten fields first, and only if that cannot fit, keep fewer list rows.
Sixteen whole matches beat two hundred matches of forty characters. A fixed number of passes, each starting from the
original, so the output is a pure function of the input — which is what makes the JS copy testable against the Python
one.

## The decision that matters: the reference is the call, not a handle

The design this borrows from spills to a store and leaves the model a handle it can dereference. That was **not**
taken here, for two reasons.

**A core-side store is data at rest.** Raw sandbox output written to the key-holding VM is the same hazard
`docs/PI-FEATURES.md` refuses for provider payload dumps: unredacted model-visible bytes on disk, outside
`output_redactor`, which `server.py` applies only at the Loom boundary.

**It is also not needed, because refetching here is expensive.** Every tool that can return a large result can return
a narrower one: `read` takes `offset` and `limit` and reports `total_lines`, `grep` takes a tighter pattern. So the
reference is a sentence the model can act on — "elided 180,000 of 250,000 chars; re-issue with offset/limit" — and
nothing is stored. In DeepSeek's design a refetch is a local file read; in Enclave it is a sandbox job behind one
serial lane, and in box mode a reboot, on a box that was reset since. A refetch has to be rare and chosen, so the
policy never issues one on the model's behalf and there is no automatic expand-the-hint loop.

A handle would also need a name in the tool list, and the loop asserts that the `tools` array holds only desk tool
names (`docs/PI-FEATURES.md`). Not adding a tool keeps that assertion honest.

## Field-aware, on purpose

Only string leaves are cut. `exit_code`, `timed_out`, `total_lines`, `truncated` and `error` pass through untouched,
so a result that is marked incomplete stays marked: the model can see from `total_lines` that there is more, and from
`exit_code` what happened. A blind cut of the serialised form would drop exactly those fields, which is the failure
mode this is meant to prevent.

Every cut is marked with how much was elided and what to do next, and a result with nothing worth cutting (a huge
object of tiny keys) is hard-cut and flagged `hard_cut` — over budget is never silent.

## What it does not do

- **It bounds one result, not a conversation.** Many medium results still add up; that is compaction's job, not this.
- **It does not reduce sandbox work.** A refetch is a new job, and the marker is written to be worth obeying rather
  than free.
- **It does not redact.** Cutting is not a security control: what is kept is still raw tool output in the context, and
  redaction remains at the Loom boundary.
- **It does not touch the user's prompt or the model's own output**, only what a tool hands back.

## Changing the budget

`PI_INLINE_MAX_CHARS` on the bridge's environment, read once at extension load; `inline.py` takes `max_chars` as an
argument, default 16,000. `FIELD_FLOOR` (512) is the size below which a marker costs more than it saves, and no
configuration can make the policy produce a marker for a smaller field.

## Tests

```
python3 -m unittest discover -s harness/toolsadapter -p 'test_inline.py'
node --test harness/bridge/tests/desk-extension-inline.test.mjs
```

The invariants: the budget is a ceiling; the completeness fields survive; head and tail are both kept; a cut is
marked; lists are capped with one marker; the same input gives the same output; the argument is not mutated. The last
JS test runs `inline.py` on the same fixtures and compares the decision, so the two copies cannot drift apart
silently.
