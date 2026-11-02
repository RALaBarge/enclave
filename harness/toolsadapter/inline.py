#!/usr/bin/env python3
"""Inline & Spill for desk tool results: bound what reaches the model, keep a way back.

The desk's tools already bound themselves — `read` answers at most 250,000
characters and reports `total_lines`, `grep` caps at 500 matches and sets
`truncated`, `bash` cuts stdout and stderr at 30,000 characters each. What none
of them can do is decide how much of that belongs in the next request. A single
`read` of a large file injects a quarter of a megabyte into the context window
and dilutes every instruction around it. This module is that decision.

Two operations, borrowed from cache management:

- **Inline** — what is likely to be used now goes into the window verbatim.
- **Spill** — what is large or unlikely stays out; the window keeps a reference
  and the model asks for a slice.

The reference here is deliberately *not* a blob handle. A handle would need a
durable store in the core, and raw sandbox output on the core's disk is the
data-at-rest hazard `docs/PI-FEATURES.md` refuses for provider payload dumps.
It is also unnecessary: every tool that can return a large result can return a
narrower one, so the reference is the call itself. "Elided 180,000 characters;
re-issue with offset=2000" is a reference the model can act on, costs one line,
and stores nothing. That is the whole adaptation — in DeepSeek's design a
refetch is a local file read, but here it is a sandbox job behind a serial lane
and (in box mode) a reboot, so a refetch must be *rare and chosen*, never
automatic.

Field-aware on purpose: only string leaves are cut, so `exit_code`, `timed_out`,
`total_lines`, `truncated` and `error` survive intact. A blind cut of the
serialised form would drop exactly the fields that tell the model whether the
result is complete.

Standard library only, and no imports from this repo: the file can be copied
next to a harness the way `desk_tools.py` is. It is the reference for the
JavaScript mirror in `harness/bridge/desk-extension.js` and for the Go client
described in `docs/PI-FEATURES.md`, the same role `desk_tools.py` plays for the
tools array.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

# A result at or under this serialises to roughly 4k tokens. Chosen to sit well
# below the point where a single tool result crowds a turn, and above the size
# of an ordinary `read`, `grep` or `bash` answer, so truncation stays rare.
MAX_CHARS_DEFAULT = 16_000

# A string shorter than this is not worth a marker: the marker costs context,
# and cutting a 400-character field saves nothing anyone will notice.
FIELD_FLOOR = 512

# Share of a kept field's budget that goes to the head. The head carries the
# imports, the signatures and the first errors; the tail carries the summary and
# the final exit status. Both beat a middle no one reads.
HEAD_RATIO = 0.6

# A list longer than this is cut when the result is over budget, so a `grep` with
# 500 matches cannot force 500 markers.
MAX_ITEMS = 200


@dataclass
class InlineInfo:
    """What `render` did, for a log line or a UI detail — never for the model."""

    original_chars: int
    final_chars: int
    elided_chars: int = 0
    fields_elided: list[str] = field(default_factory=list)
    items_elided: int = 0
    hard_cut: bool = False

    @property
    def spilled(self) -> bool:
        return bool(self.elided_chars or self.items_elided or self.hard_cut)

    def as_dict(self) -> dict:
        return {
            "original_chars": self.original_chars,
            "final_chars": self.final_chars,
            "elided_chars": self.elided_chars,
            "fields_elided": list(self.fields_elided),
            "items_elided": self.items_elided,
            "hard_cut": self.hard_cut,
        }


def marker(elided: int, total: int, *, hint: str) -> str:
    """The reference left in place of elided characters.

    Short, unambiguous, and it names the action: a model that reads "narrow the
    call" and has `offset`/`limit` in the tool's own schema knows what to do
    next, and one that reads a bare "..." does not.
    """
    return f"\n…[{elided} of {total} chars elided; {hint}]\n"


LIST_MARKER = "…[{n} more items elided; narrow the call to see them]"


def _dumps(value) -> str:
    """The compact, deterministic form used for every size in this module."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _size(value) -> int:
    return len(_dumps(value))


def _cut(text: str, cap: int, total: int, *, hint: str) -> str:
    """Head and tail of `text`, with a marker between them, at most ~`cap` chars.

    The head ends at a line break when one is available within the budget, so a
    cut file does not start mid-token; the tail starts after one for the same
    reason. If neither exists the cut is exact, because a one-line result is
    better truncated than dropped.
    """
    if len(text) <= cap:
        return text
    body = max(0, cap - len(marker(len(text) - cap, total, hint=hint)))
    if body <= 0:
        return text[:cap]
    head_len = int(body * HEAD_RATIO)
    tail_len = body - head_len
    head = text[:head_len]
    break_at = head.rfind("\n")
    if break_at > head_len // 2:
        head = head[:break_at]
    tail = text[len(text) - tail_len:] if tail_len else ""
    if tail:
        resume = tail.find("\n")
        if 0 <= resume < tail_len // 2:
            tail = tail[resume + 1:]
    elided = len(text) - len(head) - len(tail)
    return head + marker(elided, len(text), hint=hint) + tail


def _hint_for(path: str) -> str:
    """What to tell the model to do instead. The path names the field.

    An element of a list arrives as `matches[3]`, so the index is stripped
    before matching: the advice belongs to the field, not to the row.
    """
    leaf = path.rsplit(".", 1)[-1] if path else ""
    leaf = re.sub(r"(\[\d+\])+$", "", leaf)
    if leaf in ("content", "stdout", "stderr", "text", "output"):
        return "re-issue with offset/limit, or a command that prints less"
    if leaf == "matches":
        return "tighten the pattern or set a narrower path"
    return "re-issue the call with a narrower range"


def _rewrite(value, fn, path=""):
    """Rebuild `value` with `fn(path, string)` applied to each string leaf.

    A rebuild rather than an in-place walk: `shrink` runs this more than once
    from the same original, and a function that mutates makes the second pass
    depend on the first.
    """
    if isinstance(value, str):
        return fn(path, value)
    if isinstance(value, dict):
        return {key: _rewrite(item, fn, f"{path}.{key}" if path else str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, fn, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def _cap_lists(value, limit: int, info: InlineInfo):
    """Drop the tail of any list over `limit`, leaving one marker item.

    Only applied to the lists a contract actually declares (`matches` is the one
    that exists today). A marker string in a list of strings is visible and
    unambiguous; a silently shorter list is neither.
    """
    if isinstance(value, list):
        kept = value
        if len(value) > limit:
            info.items_elided += len(value) - limit
            kept = list(value[:limit]) + [LIST_MARKER.replace("{n}", str(len(value) - limit))]
        return [_cap_lists(item, limit, info) for item in kept]
    if isinstance(value, dict):
        return {key: _cap_lists(item, limit, info) for key, item in value.items()}
    return value


def shrink(value, max_chars: int = MAX_CHARS_DEFAULT) -> tuple[object, InlineInfo]:
    """Return `value` with large string leaves cut and long lists capped.

    Starts from the original each pass and halves a per-field cap until the
    serialised form fits, so the result is a pure function of the input: no
    ordering surprises, and the same result in the JS mirror and the Go client.
    """
    original = _size(value)
    info = InlineInfo(original_chars=original, final_chars=original)
    if original <= max_chars:
        return value, info

    # Two dials, in the order that loses least: first shorten the fields, and
    # only if that cannot fit, keep fewer list items. A list cut to 16 rows of
    # whole text beats 200 rows of 40 characters each, so the item limit is the
    # outer loop and the per-field cap the inner one.
    best = None
    for item_limit in (MAX_ITEMS, 50, 16, 5):
        trial = InlineInfo(original_chars=original, final_chars=original)
        base = _cap_lists(copy.deepcopy(value), item_limit, trial)
        cap = max(FIELD_FLOOR, max_chars // 2)
        while True:
            fields: list[str] = []

            def cut(path: str, text: str, cap: int = cap) -> str:
                if len(text) <= cap:
                    return text
                fields.append(path)
                return _cut(text, cap, original, hint=_hint_for(path))

            candidate = _rewrite(base, cut)
            size = _size(candidate)
            if size <= max_chars or cap <= FIELD_FLOOR:
                break
            cap = max(FIELD_FLOOR, cap // 2)
        trial.fields_elided = fields
        trial.final_chars = size
        trial.elided_chars = max(0, original - size)
        if best is None or size < best[0]:
            best = (size, candidate, trial)
        if size <= max_chars:
            break

    _, work, info = best
    return work, info


def render(value, max_chars: int = MAX_CHARS_DEFAULT) -> tuple[str, InlineInfo]:
    """The text to put in front of the model, and what was done to get it.

    Guarantees `len(text) <= max_chars`. `shrink` gets there for anything with
    string leaves to cut; a pathological result (a million tiny keys, a deeply
    nested array) has nothing worth cutting and gets a hard cut of the text
    instead — marked, and reported as `hard_cut`, so it is never silent.
    """
    original = _size(value)
    work, info = shrink(value, max_chars)
    text = work if isinstance(work, str) else _dumps(work)
    if len(text) > max_chars:
        text = _cut(text, max_chars, len(text), hint="re-issue the call with a narrower range")
        info.hard_cut = True
        info.final_chars = len(text)
        info.elided_chars = max(0, original - len(text))
    return text, info


def main(argv=None, out=None, err=None) -> int:
    """`python3 inline.py [BUDGET] < result.json` — show what a model receives."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Show the inline size of a desk tool result.")
    parser.add_argument("max_chars", nargs="?", type=int, default=MAX_CHARS_DEFAULT)
    parser.add_argument("--json", action="store_true", help="print the info as JSON on stderr")
    args = parser.parse_args(argv)
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not JSON: {exc}", file=err)
        return 1
    text, info = render(value, args.max_chars)
    out.write(text + "\n")
    if args.json:
        json.dump(info.as_dict(), err, sort_keys=True)
        err.write("\n")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
