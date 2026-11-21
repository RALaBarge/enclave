#!/usr/bin/env python3
"""fanout: ask N models the same question, then have one model synthesize them and add its own take.

stdin  {"prompt": "...", "system"?, "thinking"?: bool, "stubs"?: [{"name", "model", "key"?}], "synthesizer"?: {"backend"?, "model"?, "key"?},
        "max_tokens"?, "temperature"?, "timeout_s"?}
stdout {"synthesizer": {...}, "answers": [every model's full reply], "ok_count": n}

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
import subprocess
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
DEFAULT_SYNTH_MODEL = "anthropic/claude-fable-5.2"   # OpenRouter slug, for backend "api"
DEFAULT_CLI_MODEL = "claude-fable-5-2"               # Anthropic model id, for backend "cli"
DEFAULT_KEY = "openrouter"
MAX_STUBS = 16
MAX_NOTE_CHARS = 4000
MAX_ANSWER_CHARS = 16000     # an answer longer than this is cut; the models are told (see cap_note)
DEFAULT_TIMEOUT_S = 90

SYNTH_SYSTEM = (
    "You are the synthesizer. You are shown one question and the full answers of several named models. Do not rank or "
    "grade them. Reply with one JSON object and nothing else: "
    '{"synthesis": "<one final answer that takes the strongest parts of all of them and resolves conflicts by reasoning, '
    'not a summary of who said what>", '
    '"agreement": "<what most of the models agree on>", '
    '"differences": "<where they differ or contradict, naming the models, and which side you think is right and why>", '
    '"two_cents": "<your own view: what all of them missed or got wrong, and what you would add>"}'
)


def cap_note():
    return (f"Length limit: your reply is cut off after {MAX_ANSWER_CHARS} characters (about {MAX_ANSWER_CHARS // 4} tokens), "
            "and a cut-off answer is treated as incomplete. Put the most important content first, be complete but concise, "
            "and finish within the limit.")


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
    if content:
        return content, None
    # Reasoning models can spend the whole token budget thinking and never write an answer.
    if doc["choices"][0].get("finish_reason") == "length":
        return None, "no answer: hit max_tokens before finishing (a reasoning model? raise max_tokens)"
    return None, "empty answer"


def cli_chat(keys, model, system, user, timeout):
    """The synthesizer through Claude Code's headless mode, so it bills to the Claude Code subscription, not an API key.
    keys.json carries {"claude_cli": {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "<from `claude setup-token`>"}}}, and optionally
    "path" if the binary is not /usr/bin/claude. A token, not a login, because the job's filesystem is read-only apart
    from a tmpfs /tmp: nothing about the login has to persist or be refreshed on disk. Returns (text, error); never raises."""
    conf = keys.get("claude_cli") or {}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    env.update(conf.get("env") or {})
    cmd = [conf.get("path") or "claude", "-p", "--model", model, "--system-prompt", system, "--tools", "",
           "--no-session-persistence", "--disable-slash-commands", "--setting-sources", "",
           "--strict-mcp-config", "--output-format", "text"]
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except FileNotFoundError:
        return None, "claude CLI not found; install it on the core or set claude_cli.path in keys.json"
    except subprocess.TimeoutExpired:
        return None, f"claude CLI timed out after {timeout}s"
    if r.returncode != 0:
        return None, ("claude CLI exit %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()))[:300]
    text = r.stdout.strip()
    return (text, None) if text else (None, "empty answer")


def ask_stub(stub, keys, messages, params, timeout):
    t0 = time.time()
    entry = keys.get(stub.get("key") or DEFAULT_KEY)
    if not entry:
        text, err = None, f"no keys.json entry named {stub.get('key') or DEFAULT_KEY!r}"
    else:
        text, err = chat(entry, stub["model"], messages, params, timeout)
    return {"name": stub["name"], "model": stub["model"], "ok": text is not None,
            "text": (text or "")[:MAX_ANSWER_CHARS], "error": err or "", "ms": int((time.time() - t0) * 1000)}


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def empty_synth(model, error=""):
    return {"model": model, "synthesis": "", "agreement": "", "differences": "", "two_cents": "", "error": error}


def synthesize(question, good, keys, spec, params, timeout):
    """good: the ok answers. The synthesizer sees the model names so it can compare and contrast them."""
    listing = "\n\n".join(f"--- {a['name']} ({a['model']}) ---\n{a['text']}" for a in good)
    user = f"Question:\n{question}\n\n{listing}"
    cli = spec.get("backend") == "cli"
    model = spec.get("model") or (DEFAULT_CLI_MODEL if cli else DEFAULT_SYNTH_MODEL)
    out = empty_synth(model)
    if cli:
        text, err = cli_chat(keys, model, SYNTH_SYSTEM, user, timeout)
    else:
        entry = keys.get(spec.get("key") or DEFAULT_KEY)
        if not entry:
            out["error"] = f"no keys.json entry named {spec.get('key') or DEFAULT_KEY!r}"
            return out
        messages = [{"role": "system", "content": SYNTH_SYSTEM}, {"role": "user", "content": user}]
        text, err = chat(entry, model, messages, params, timeout)
    if err:
        out["error"] = err
        return out
    doc = parse_json(text)
    if doc is None:       # keep what it said rather than lose it
        out["error"] = "synthesizer did not return JSON; its raw text is in synthesis"
        out["synthesis"] = text[:MAX_ANSWER_CHARS]
        return out
    out["synthesis"] = str(doc.get("synthesis", ""))[:MAX_ANSWER_CHARS]
    for k in ("agreement", "differences", "two_cents"):
        out[k] = str(doc.get(k, ""))[:MAX_NOTE_CHARS]
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
    if not req.get("thinking"):
        # OpenRouter's unified switch. Reasoning models otherwise spend the token budget thinking and can return nothing.
        # It is part of the shared body, so every stub gets it; models without reasoning ignore it.
        params["reasoning"] = {"enabled": False}
    if req.get("max_tokens"):
        params["max_tokens"] = int(req["max_tokens"])
    if req.get("temperature") is not None:
        params["temperature"] = float(req["temperature"])

    system = (req["system"].rstrip() + "\n\n" if req.get("system") else "") + cap_note()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    keys = load_keys()
    with ThreadPoolExecutor(max_workers=len(stubs)) as pool:
        answers = list(pool.map(lambda s: ask_stub(s, keys, messages, params, timeout), stubs))
    good = [a for a in answers if a["ok"]]

    spec = req.get("synthesizer") or {}
    if spec.get("backend", "api") not in ("api", "cli", "none"):
        die("synthesizer.backend must be api, cli or none")
    if spec.get("backend") == "none":     # answers only: the caller reads and combines them itself
        synth = empty_synth("")
    elif good:
        synth = synthesize(prompt, good, keys, spec, {"max_tokens": 8000}, timeout)
    else:
        synth = empty_synth(spec.get("model") or DEFAULT_SYNTH_MODEL, "no model returned an answer")
    # `answers` is always every model's full reply, in the order asked, so they can be compared directly.
    json.dump({"synthesizer": synth, "answers": answers, "ok_count": len(good)}, sys.stdout)


if __name__ == "__main__":
    main()
