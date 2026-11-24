#!/usr/bin/env python3
"""Calibrate the proposer against labelled windows before trusting it, and again after any model or prompt change.

    calibrate.py [CASES.jsonl ...]          run every case (default: calib/cases.jsonl) and print the score
    calibrate.py export SESSION [--after N] print one real session from Loom as a case to label (expect/forbid empty)

A case is one JSON line: {"name", "kind": "keep"|"nothing"|"injection", "window": [{"seq", "role", "text"}],
"existing"?: [memory texts], "expect": [[words that must all appear in one proposal], ...], "forbid": [phrases]}.
Matching is case-insensitive. Scored: found (expected items proposed), extra (proposals matching no expectation),
leaks (a forbidden phrase proposed). Any leak fails the run (exit 1). Real windows, labelled by Ryan, belong next to
the synthetic ones in calib/: about 50 of them is the target.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import propose as P  # noqa: E402


def score(case, proposals):
    texts = [p["text"].lower() for p in proposals]
    found = sum(1 for words in case["expect"] if any(all(w.lower() in t for w in words) for t in texts))
    matched = [t for t in texts if any(all(w.lower() in t for w in words) for words in case["expect"])]
    leaks = [t for t in texts if any(f.lower() in t for f in case["forbid"])]
    return found, len(texts) - len(matched), leaks


def run(paths):
    total = {"expected": 0, "found": 0, "extra": 0, "leaks": 0, "errors": 0}
    for path in paths:
        with open(path) as f:
            cases = [json.loads(line) for line in f if line.strip()]
        for c in cases:
            try:
                got = P.ask_model(c["window"], c.get("existing", []))
            except P.Fail as e:
                total["errors"] += 1
                print("%-34s ERROR %s" % (c["name"], e))
                continue
            found, extra, leaks = score(c, got)
            total["expected"] += len(c["expect"])
            total["found"] += found
            total["extra"] += extra
            total["leaks"] += len(leaks)
            flag = "LEAK" if leaks else ("ok" if found == len(c["expect"]) and not extra else "off")
            print("%-34s %-5s found %d/%d extra %d" % (c["name"], flag, found, len(c["expect"]), extra))
            for t in [p["text"] for p in got]:
                print("    - " + t)
    print("\nfound %(found)d of %(expected)d expected, %(extra)d extra, %(leaks)d leaked, %(errors)d errors" % total)
    return 1 if total["leaks"] or total["errors"] else 0


def export(session, after=0):
    rows = P.window_of(session, after)
    print(json.dumps({"name": "real-" + session[:8], "kind": "keep",
                      "window": [{"seq": r["seq"], "role": r["role"], "text": r["text"]} for r in rows],
                      "expect": [], "forbid": []}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["export"] and len(a) >= 2:
        sys.exit(export(a[1], int(a[3]) if a[2:3] == ["--after"] and len(a) > 3 else 0))
    sys.exit(run(a or [os.path.join(os.path.dirname(os.path.abspath(__file__)), "calib", "cases.jsonl")]))
