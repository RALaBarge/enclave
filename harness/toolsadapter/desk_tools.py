#!/usr/bin/env python3
"""Turn the desk's tool list into a harness's tool definitions.

The desk is the only executor. This module asks it what tools exist and emits
the shapes a harness can load; it never runs a tool and never reads a run
script, so a second harness needs no tool code of its own.

DeskBox at DESKBOX_COMMIT (`image/manifest.env`) splits the contract across two
routes: `GET /tools` lists each tool's name, summary and execution fields, while
`GET /tools/{name}` returns that tool's `tcs.yaml` re-decoded as JSON, including
the input and output schemas. `fetch()` therefore makes one detail request per
tool. Calls go back to `POST /tools/{name}` with `{"input": {...}}` and the
optional `?wait=<duration>`.

Only the standard library is used: the file is copied next to a harness, not
installed from a package index.
"""
from __future__ import annotations

import argparse
import copy
import http.client
import json
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}}
FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# A queued tool's `?wait=` is capped at 55 s and the desk answers 202 when the
# wait elapses, so 70 s covers that path plus scheduling. A tool declared
# `execution.mode: direct` ignores `wait`: the desk holds the request for the
# tool's own timeout budget, so a caller using one passes a larger `timeout`
# (the broker caps a job at 120 s plus grace).
CALL_TIMEOUT = 70.0


class DeskError(RuntimeError):
    """The desk could not be used: an HTTP error, a failed job, or a bad answer."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None, payload: object = None):
        super().__init__(message)
        self.url = url
        self.status = status
        self.payload = payload


class DeskUnreachable(DeskError):
    """No HTTP response arrived: connection refused, timed out, DNS, reset."""


class DeskDataError(DeskError):
    """The desk answered, but not with data this adapter can express."""


def _base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("desk base URL must be a non-empty string")
    base = base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"desk base URL must start with http:// or https://: {base_url!r}")
    return base


def _tool_path(name: str) -> str:
    return "/tools/" + quote(name, safe="")


def _http_error_body(exc: urllib.error.HTTPError) -> tuple[str, object]:
    """The desk's own error text from an HTTP error body, when it sent one."""
    try:
        raw = exc.read()
    except (OSError, http.client.HTTPException):
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict):
        detail = payload.get("error")
        if not isinstance(detail, str) or not detail:
            detail = text
        violations = payload.get("violations")
        if violations:
            detail += "; violations: " + json.dumps(violations, sort_keys=True)
    else:
        detail = text
    return (detail or "no error message"), payload


def _request_json(url: str, *, timeout: float, data: bytes | None = None):
    """GET, or POST when data is given, and decode a JSON response.

    HTTP errors keep the desk's `error` (and `violations`) text. Anything that
    stops a response arriving is DeskUnreachable and names the exact URL, so an
    operator can tell a stopped desk from a desk that answered badly.
    """
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail, payload = _http_error_body(exc)
        raise DeskError(
            f"desk at {url} returned HTTP {exc.code}: {detail}",
            url=url,
            status=exc.code,
            payload=payload,
        ) from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise DeskUnreachable(f"desk unreachable at {url}: {reason}", url=url) from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        snippet = raw[:200].decode("utf-8", "replace")
        raise DeskDataError(f"desk at {url} returned a body that is not JSON: {snippet!r}", url=url) from exc


def _input_schema(raw, *, url: str, name: str) -> dict:
    """The tool's input schema, always usable as function `parameters`.

    A tool with no input schema, or an empty one, still gets a valid
    empty-object schema: OpenAI-compatible function definitions require
    parameters to be a JSON object, and the desk's POST body accepts `input`
    only as a JSON object. An absent `type` is filled for the same reason; a
    type other than object is a contract the desk itself cannot submit to.
    """
    if raw is None or raw == {}:
        return copy.deepcopy(EMPTY_OBJECT_SCHEMA)
    if not isinstance(raw, dict):
        raise DeskDataError(
            f"desk at {url} returned {name!r} with an input schema that is not an object: {raw!r}", url=url
        )
    properties = raw.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise DeskDataError(
            f"desk at {url} returned {name!r} with input properties that are not an object: {properties!r}", url=url
        )
    required = raw.get("required")
    if required is not None and not (isinstance(required, list) and all(isinstance(item, str) for item in required)):
        raise DeskDataError(
            f"desk at {url} returned {name!r} with an input required list that is not a list of strings: {required!r}",
            url=url,
        )
    schema = copy.deepcopy(raw)
    kind = schema.get("type")
    if kind is not None and kind != "object":
        raise DeskDataError(
            f"desk at {url} returned {name!r} with input type {kind!r}; the desk submits input as a JSON object",
            url=url,
        )
    if kind is None and "oneOf" not in schema and "anyOf" not in schema:
        schema["type"] = "object"
    return schema


def _output_schema(raw, *, url: str, name: str) -> dict:
    """The declared output schema, or {} when the contract declares none.

    Output is not part of a function definition, and the desk's validator
    treats an empty schema as unconstrained, so nothing is invented here.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DeskDataError(
            f"desk at {url} returned {name!r} with an output section that is not an object: {raw!r}", url=url
        )
    schema = raw.get("schema")
    if schema is None:
        return {}
    if not isinstance(schema, dict):
        raise DeskDataError(
            f"desk at {url} returned {name!r} with an output schema that is not an object: {schema!r}", url=url
        )
    return copy.deepcopy(schema)


def _normalise(entry: dict, contract: object, *, detail_url: str) -> dict:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise DeskDataError(f"desk at {detail_url} returned a tool with no usable name: {name!r}", url=detail_url)
    if not isinstance(contract, dict):
        raise DeskDataError(
            f"desk at {detail_url} returned {type(contract).__name__}, expected the tool contract object",
            url=detail_url,
        )
    contract_name = contract.get("name")
    if contract_name is not None and contract_name != name:
        raise DeskDataError(
            f"desk at {detail_url} returned the contract for {contract_name!r}, not {name!r}", url=detail_url
        )
    summary = contract.get("summary", entry.get("summary", ""))
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        raise DeskDataError(
            f"desk at {detail_url} returned a non-string summary for {name!r}: {summary!r}", url=detail_url
        )
    return {
        "name": name,
        "summary": summary,
        "input_schema": _input_schema(contract.get("input"), url=detail_url, name=name),
        "output_schema": _output_schema(contract.get("output"), url=detail_url, name=name),
        "invoke": {"method": "POST", "path": _tool_path(name)},
    }


def fetch(base_url: str, timeout=5) -> list[dict]:
    """Ask the desk for its tools and return them normalised, sorted by name.

    One request lists the tools; because that route omits the contract's
    input and output schemas, one more request per tool reads them. `timeout`
    applies to each request, not to the whole fetch.
    """
    base = _base_url(base_url)
    list_url = base + "/tools"
    listed = _request_json(list_url, timeout=timeout)
    if not isinstance(listed, list):
        raise DeskDataError(
            f"desk at {list_url} returned {type(listed).__name__}, expected a JSON array of tools", url=list_url
        )
    tools = []
    seen = set()
    for entry in listed:
        if not isinstance(entry, dict):
            raise DeskDataError(
                f"desk at {list_url} listed {type(entry).__name__}, expected a tool object", url=list_url
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise DeskDataError(f"desk at {list_url} listed a tool with no usable name: {name!r}", url=list_url)
        if name in seen:
            raise DeskDataError(f"desk at {list_url} listed {name!r} twice", url=list_url)
        seen.add(name)
        detail_url = base + _tool_path(name)
        contract = _request_json(detail_url, timeout=timeout)
        tools.append(_normalise(entry, contract, detail_url=detail_url))
    tools.sort(key=lambda tool: tool["name"])
    return tools


def call(base_url: str, name: str, arguments, wait="45s", timeout=CALL_TIMEOUT):
    """POST one tool call and return the job's `result`.

    Raises DeskError with the desk's own error text for an HTTP error, a failed
    or canceled job, or a job still queued/running when `wait` elapsed. There is
    no polling loop here; a caller that wants to keep waiting can pass a longer
    `wait` (the desk caps it at 55 s) or poll `GET /jobs/{id}` itself.

    `timeout` bounds the HTTP request. The default covers queued tools; a
    direct-mode tool holds the request for its own timeout budget instead.
    """
    base = _base_url(base_url)
    if not isinstance(name, str) or not name:
        raise ValueError("tool name must be a non-empty string")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError(f"arguments must be a JSON object, got {type(arguments).__name__}")
    url = base + _tool_path(name)
    if wait:
        if not isinstance(wait, str):
            raise ValueError(f"wait must be a Go duration string such as '45s', got {wait!r}")
        url += "?" + urlencode({"wait": wait})
    job = _request_json(url, timeout=timeout, data=json.dumps({"input": arguments}).encode())
    if not isinstance(job, dict):
        raise DeskDataError(f"desk at {url} returned {type(job).__name__}, expected a job object", url=url)
    status = job.get("status")
    job_id = job.get("id", "?")
    if status == "done":
        # A tool whose output is JSON null leaves `result` omitted (the job
        # field is omitempty), so a missing key is null, not a failed call.
        return job.get("result")
    if status in ("failed", "canceled"):
        detail = job.get("error") or "no error message"
        raise DeskError(f"desk at {url} job {job_id} {status}: {detail}", url=url, payload=job)
    if status in ("queued", "running"):
        raise DeskError(
            f"desk at {url} job {job_id} was still {status} after wait={wait or '0'}; "
            f"poll GET /jobs/{job_id} (the desk caps wait at 55s)",
            url=url,
            payload=job,
        )
    raise DeskDataError(f"desk at {url} returned job {job_id} with unknown status {status!r}", url=url, payload=job)


def as_openapi_functions(tools: list[dict]) -> list[dict]:
    """The function-definition shape OpenAI-compatible harnesses accept.

    The desk's names are used unchanged. A name this shape cannot carry is an
    error rather than a silent rename: a rename would leave the model calling a
    tool the desk does not have.
    """
    functions = []
    for tool in tools:
        name = tool["name"]
        if not FUNCTION_NAME_RE.match(name):
            raise DeskDataError(
                f"tool name {name!r} cannot be an OpenAI-compatible function name "
                "(letters, digits, '_' and '-', at most 64 characters); rename the tool on the desk",
                url=tool.get("invoke", {}).get("path", ""),
            )
        functions.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["summary"],
                    "parameters": copy.deepcopy(tool["input_schema"]),
                },
            }
        )
    return functions


def as_text(tools: list[dict]) -> str:
    """A prompt-embedded listing: name, one-line summary, input schema."""
    if not tools:
        return "Tools: none.\n"
    lines = ["Tools (executed by the desk; one tool per call):", ""]
    for tool in tools:
        summary = " ".join(tool["summary"].split())
        lines.append(f"{tool['name']}: {summary}" if summary else tool["name"])
        lines.append("  input: " + json.dumps(tool["input_schema"], sort_keys=True))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None, out=None, err=None) -> int:
    parser = argparse.ArgumentParser(description="Show the tool definitions the desk would give a harness.")
    parser.add_argument("--desk", default="http://127.0.0.1:8080", help="desk base URL (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=5, help="seconds per desk request (default: %(default)s)")
    parser.add_argument(
        "mode",
        choices=("list", "openapi", "text"),
        help="list: normalised JSON; openapi: function definitions; text: prompt listing",
    )
    args = parser.parse_args(argv)
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    try:
        tools = fetch(args.desk, timeout=args.timeout)
        if args.mode == "list":
            json.dump(tools, out, indent=2)
            out.write("\n")
        elif args.mode == "openapi":
            json.dump(as_openapi_functions(tools), out, indent=2)
            out.write("\n")
        else:
            out.write(as_text(tools))
    except (DeskError, ValueError) as exc:
        print(f"error: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
