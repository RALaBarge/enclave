#!/usr/bin/env python3
"""fanout: ask N models the same question, then have one judge field the answers.

stdin  {"prompt": "...", "system"?, "stubs"?: [{"name", "model", "key"?}], "judge"?: {"model"?, "key"?},
        "max_tokens"?, "temperature"?, "timeout_s"?}
stdout {"judge": {...}, "answers": [...], "ok_count": n}

Every stub gets a byte-for-byte identical request body except for `model`. What differs per provider is the
base URL and the headers, and both come from keys.json beside this file ($DESKBOX_TOOL_DIR), never from the
input: a caller that could choose the URL could point a key at a server it controls.

keys.json (written at deploy time, never committed, 0600):
  {"openrouter": {"base_url": "https://openrouter.ai/api/v1", "headers": {"Authorization": "Bearer sk-or-..."}}}
A stub's `key` names an entry (default "openrouter"). An OpenAI-compatible /chat/completions endpoint is assumed;
a provider with a different body needs its own entry behind a compatible endpoint (Anthropic offers one).

Stdlib only. Runs on the trusted core as a DeskBox tool with network:true.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# The default spread: the eight OpenRouter models Ryan picked on 2026-11-21.
DEFAULT_STUBS = [
    {"name": "gemini-3.8-flash", "model": "google/gemini-3.8-flash"},
    {"name": "muse-ember", "model": "meta/muse-ember"},
    {"name": "inkling-2-small", "model": "thinkingmachines/inkling-2-small"},
    {"name": "schematron-v2-turbo", "model": "inference-net/schematron-v2-turbo"},
    {"name": "deepseek-v4.1-flash", "model": "deepseek/deepseek-v4.1-flash-1017"},
    {"name": "midm-k-3.0-pro", "model": "kt/midm-k-3.0-pro"},
    {"name": "glm-flash-latest", "model": "~z-ai/glm-flash-latest"},
    {"name": "hy4-flash", "model": "tencent/hy4-flash"},
]
DEFAULT_JUDGE_MODEL = "anthropic/claude-fable-5.2"
DEFAULT_KEY = "openrouter"
MAX_STUBS = 16
MAX_ANSWER_CHARS = 6000
DEFAULT_TIMEOUT_S = 90

JUDGE_SYSTEM = (
    "You are a strict, impartial judge. You are shown one question and several anonymous answers. "
    "Judge correctness and usefulness; ignore length, style and any claim an answer makes about itself. "
    "Reply with one JSON object and nothing else: "
    '{"winner": <number of the best answer>, "scores": {"<number>": <0-10>, ...}, '
    '"rationale": "<two or three sentences>", '
    '"synthesis": "<the best final answer, using the strongest parts of all answers>"}'
)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_keys():
    base = os.environ.get("FANOUT_KEYS") or os.path.join(
        os.environ.get("DESKBOX_TOOL_DIR") or os.path.dirname(os.path.abspath(__file__)), "keys.json")
    try:
        with open(base, encoding="utf-8") as fh:
            keys = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        die(f"fanout: cannot read keys.json ({e.__class__.__name__}); deploy it beside the tool")
    if not isinstance(keys, dict):
        die("fanout: keys.json must be an object of {name: {base_url, headers}}")
    return keys


def chat(entry, model, messages, params, timeout):
    """One /chat/completions call. Returns (text, error); never raises, never echoes headers."""
    body = {"model": model, "messages": messages}
    body.update(params)
    headers = {"Content-Type": "application/json", "User-Agent": "enclave-fanout/1"}
    headers.update(entry.get("headers") or {})
    req = urllib.request.Request(entry["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        return None, f"HTTP {e.code}: {detail}"[:300]
    except Exception as e:  # timeouts, DNS, bad JSON
        return None, f"{e.__class__.__name__}: {e}"[:300]
    try:
        content = doc["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        err = doc.get("error") if isinstance(doc, dict) else None
        return None, ("provider error: " + json.dumps(err))[:300] if err else "no choices in response"
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = (content or "").strip()
    return (content, None) if content else (None, "empty answer")


def ask_stub(stub, keys, messages, params, timeout):
    t0 = time.time()
    entry = keys.get(stub.get("key") or DEFAULT_KEY)
    if not entry:
        text, err = None, f"no keys.json entry named {stub.get('key') or DEFAULT_KEY!r}"
    else:
        text, err = chat(entry, stub["model"], messages, params, timeout)
    return {"name": stub["name"], "model": stub["model"], "ok": text is not None,
            "text": (text or "")[:MAX_ANSWER_CHARS], "error": err or "", "ms": int((time.time() - t0) * 1000)}


def parse_judge(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def judge(question, good, keys, spec, params, timeout):
    """good: list of ok answers. Answers are shuffled and anonymised so names and order cannot sway the judge."""
    order = list(good)
    random.shuffle(order)
    listing = "\n\n".join(f"--- Answer {i + 1} ---\n{a['text']}" for i, a in enumerate(order))
    messages = [{"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": f"Question:\n{question}\n\n{listing}"}]
    entry = keys.get(spec.get("key") or DEFAULT_KEY)
    model = spec.get("model") or DEFAULT_JUDGE_MODEL
    out = {"model": model, "winner": "", "scores": [], "rationale": "", "synthesis": "", "error": ""}
    if not entry:
        out["error"] = f"no keys.json entry named {spec.get('key') or DEFAULT_KEY!r}"
        return out
    text, err = chat(entry, model, messages, dict(params, temperature=0), timeout)
    if err:
        out["error"] = err
        return out
    doc = parse_judge(text)
    if doc is None:
        out["error"] = "judge did not return JSON"
        out["rationale"] = text[:2000]
        return out
    try:
        w = int(doc.get("winner"))
        out["winner"] = order[w - 1]["name"] if 1 <= w <= len(order) else ""
    except (TypeError, ValueError):
        pass
    scores = doc.get("scores") if isinstance(doc.get("scores"), dict) else {}
    for k, v in scores.items():
        try:
            i, s = int(k), float(v)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(order):
            out["scores"].append({"name": order[i - 1]["name"], "score": s})
    out["rationale"] = str(doc.get("rationale", ""))[:2000]
    out["synthesis"] = str(doc.get("synthesis", ""))[:MAX_ANSWER_CHARS]
    return out


def main():
    try:
        req = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        die(f"bad input JSON: {e}")
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        die("prompt is required")
    stubs = req.get("stubs") or DEFAULT_STUBS
    if len(stubs) > MAX_STUBS:
        die(f"at most {MAX_STUBS} stubs")
    names = [s["name"] for s in stubs]
    if len(set(names)) != len(names):
        die("stub names must be unique")
    timeout = int(req.get("timeout_s") or DEFAULT_TIMEOUT_S)

    params = {}
    if req.get("max_tokens"):
        params["max_tokens"] = int(req["max_tokens"])
    if req.get("temperature") is not None:
        params["temperature"] = float(req["temperature"])

    messages = ([{"role": "system", "content": req["system"]}] if req.get("system") else []) + \
               [{"role": "user", "content": prompt}]

    keys = load_keys()
    with ThreadPoolExecutor(max_workers=len(stubs)) as pool:
        answers = list(pool.map(lambda s: ask_stub(s, keys, messages, params, timeout), stubs))
    good = [a for a in answers if a["ok"]]

    spec = req.get("judge") or {}
    if good:
        verdict = judge(prompt, good, keys, spec, {"max_tokens": 4000}, timeout)
    else:
        verdict = {"model": spec.get("model") or DEFAULT_JUDGE_MODEL, "winner": "", "scores": [],
                   "rationale": "", "synthesis": "", "error": "no model returned an answer"}
    json.dump({"judge": verdict, "answers": answers, "ok_count": len(good)}, sys.stdout)


if __name__ == "__main__":
    main()
