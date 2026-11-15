"""Walk Enclave (+ the tools submodule) and deskbox at the commit image/manifest.env pins; print the graph as JSON:
files, symbols (functions, methods, classes, types), calls between them, and names used at module level."""
import ast, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENC = os.path.dirname(HERE)                                   # the enclave checkout this file lives in
DBX = os.environ.get("DESKBOX_REPO", os.path.join(os.path.dirname(ENC), "deskbox"))   # a deskbox clone, any branch
DBX_REV = re.search(r"^DESKBOX_COMMIT=(\w+)", open(os.path.join(ENC, "image", "manifest.env")).read(), re.M).group(1)[:7]

def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout

def enclave_files():
    out = []
    for p in sh("git", "ls-files", "--recurse-submodules", cwd=ENC).splitlines():
        if p.startswith((".", "docs/", "atlas/")) or p.endswith((".md", ".gitkeep", ".txt")) or p in ("LICENSE",):
            continue
        try:
            out.append((p, open(os.path.join(ENC, p), encoding="utf-8", errors="replace").read()))
        except OSError:
            pass
    return out

def deskbox_files():
    out = []
    for p in sh("git", "ls-tree", "-r", "--name-only", DBX_REV, cwd=DBX).splitlines():
        if p.endswith(".go"):
            out.append(("deskbox/" + p, sh("git", "show", f"{DBX_REV}:{p}", cwd=DBX)))
    return out

def lang(path, text):
    ext = os.path.splitext(path)[1].lower()
    first = text.split("\n", 1)[0]
    if ext == ".py" or "python" in first: return "python"
    if ext in (".js", ".mjs"): return "js"
    if ext == ".html": return "html"
    if ext in (".sh",) or re.search(r"\b(ba)?sh\b", first) and first.startswith("#!"): return "shell"
    if ext == ".go": return "go"
    if ext == ".ps1": return "powershell"
    if ext in (".yaml", ".yml"): return "yaml"
    if ext == ".service": return "systemd"
    if ext == ".hcl": return "hcl"
    return ext.lstrip(".") or "text"

def component(path):
    parts = path.split("/")
    if parts[0] == "deskbox":
        if len(parts) > 2 and parts[1] in ("cmd", "internal", "pkg"):
            return "deskbox/" + parts[1] + "/" + parts[2].replace(".go", "")
        return "deskbox"
    if parts[0] == "tools":
        return "tools/" + parts[1] if len(parts) > 2 else "tools"
    if parts[0] == "harness":
        return "harness/" + parts[1]
    if len(parts) == 1:
        return "root"
    return parts[0]

KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "else", "do", "try"}

symbols, calls, files = [], [], []

def add_sym(fid, name, kind, line, end, sig="", doc="", parent=None):
    sid = f"{fid}#{parent + '.' if parent else ''}{name}@{line}"
    symbols.append({"id": sid, "file": fid, "name": name, "kind": kind, "line": line, "end": end,
                    "sig": sig[:200], "doc": (doc or "").strip().split("\n\n")[0].replace("\n", " ")[:400],
                    "parent": parent})
    return sid

# ---------------- python: calls carry their shape so they can be resolved by scope
MODALIAS = {}
MODCALLS = {}
def py(fid, text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    aliases = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names: aliases[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names: aliases[a.asname or a.name] = n.module + ":" + a.name
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call) \
                and getattr(n.value.func, "attr", "") in ("module_from_spec", "import_module", "load_module"):
            aliases[n.targets[0].id] = "?"
    MODALIAS[fid] = aliases
    local = []
    def sig_of(n):
        try: return f"{n.name}({ast.unparse(n.args)})"
        except Exception: return n.name
    def walk(body, cls=None):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sid = add_sym(fid, n.name, "method" if cls else "function", n.lineno, n.end_lineno,
                              ("async " if isinstance(n, ast.AsyncFunctionDef) else "") + "def " + sig_of(n), ast.get_docstring(n), cls)
                local.append((sid, n, cls))
                walk(n.body, cls)
            elif isinstance(n, ast.ClassDef):
                bases = ", ".join(ast.unparse(x) for x in n.bases)
                add_sym(fid, n.name, "class", n.lineno, n.end_lineno, f"class {n.name}({bases})" if bases else f"class {n.name}", ast.get_docstring(n), cls)
                walk(n.body, n.name)
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                walk(getattr(n, "body", []) + getattr(n, "orelse", []) + [y for h in getattr(n, "handlers", []) for y in h.body], cls)
    walk(tree.body)
    mod = set()
    def modwalk(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in ch.decorator_list: modwalk(dec)
                continue
            if isinstance(ch, ast.Name): mod.add(ch.id)
            elif isinstance(ch, ast.Attribute): mod.add(ch.attr)
            modwalk(ch)
    modwalk(tree)
    MODCALLS[fid] = sorted(mod)
    out = []
    for sid, n, cls in local:
        refs = set()
        for c in ast.walk(n):
            if isinstance(c, ast.Call):
                f = c.func
                if isinstance(f, ast.Name): refs.add(("name", f.id, None, cls))
                elif isinstance(f, ast.Attribute):
                    base = f.value
                    if isinstance(base, ast.Name) and base.id in ("self", "cls"): refs.add(("self", f.attr, None, cls))
                    elif isinstance(base, ast.Name): refs.add(("attr", f.attr, base.id, cls))
                    elif isinstance(base, ast.Call) and getattr(base.func, "id", "") == "super": refs.add(("super", f.attr, None, cls))
                    else: refs.add(("attr", f.attr, None, cls))
            elif isinstance(c, ast.Name) and isinstance(c.ctx, ast.Load):
                refs.add(("ref", c.id, None, cls))
            elif isinstance(c, ast.Attribute) and isinstance(c.value, ast.Name) and c.value.id == "self" and isinstance(c.ctx, ast.Load):
                refs.add(("selfref", c.attr, None, cls))
        out.append((sid, refs))
    return out

# ---------------- regex languages: find defs, body = until next def at same or lower indent (approx: next def)
def regex_defs(fid, text, patterns, kind_of):
    lines = text.split("\n")
    defs = []
    for i, ln in enumerate(lines, 1):
        for pat in patterns:
            m = pat.match(ln)
            if m and m.group("name") not in KEYWORDS:
                defs.append((i, m.group("name"), ln.strip(), kind_of(m, ln)))
                break
    out = []
    if defs:
        inside = set()
        for k, (i, *_r) in enumerate(defs):
            end = defs[k + 1][0] - 1 if k + 1 < len(defs) else len(lines)
            # a shell/JS body ends at the first line that is only a closing brace at column 0
            inside.add(i)
            if re.search(r"\{.*\}\s*$", lines[i - 1]): continue    # one-liner
            for j in range(i + 1, end + 1):
                inside.add(j)
                if re.match(r"^\}", lines[j - 1]): break
        outer = "\n".join(l for n, l in enumerate(lines, 1) if n not in inside)
        MODCALLS[fid] = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", outer)))
    for k, (i, name, sigline, kind) in enumerate(defs):
        end = defs[k + 1][0] - 1 if k + 1 < len(defs) else len(lines)
        doc = ""
        j = i - 2
        cm = []
        while j >= 0 and re.match(r"^\s*(#|//)", lines[j]):
            cm.insert(0, re.sub(r"^\s*(#|//)\s?", "", lines[j])); j -= 1
        doc = " ".join(cm)
        parent = kind[1] if isinstance(kind, tuple) else None
        kname = kind[0] if isinstance(kind, tuple) else kind
        sid = add_sym(fid, name, kname, i, end, sigline, doc, parent)
        first = lines[i - 1]
        if re.search(r"\{.*\}\s*$", first):          # one-line body: def and body on the same line
            end = i
        body = first.split("{", 1)[1] if "{" in first else ""
        body += "\n" + "\n".join(lines[i:end]) if end > i else ""
        out.append((sid, set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_-]*)\s*\(", body)) | set("word:" + w for w in re.findall(r"(?:^|[\s;|&(`$])([A-Za-z_][A-Za-z0-9_-]*)", body, re.M))))
    return out

JS_PATS = [re.compile(r"^\s*(export\s+)?(async\s+)?function\*?\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("),
           re.compile(r"^\s*(export\s+)?(const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(async\s*)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
           re.compile(r"^\s*(export\s+)?(const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(async\s+)?function"),
           re.compile(r"^\s*(export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"),
           re.compile(r"^\s{2,}(async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*$")]
def js_kind(m, ln):
    if re.search(r"\bclass\s", ln): return "class"
    if re.match(r"^\s{2,}(async\s+)?[A-Za-z_$][\w$]*\s*\(", ln) and "function" not in ln and "=" not in ln: return "method"
    return "function"

SH_PATS = [re.compile(r"^\s*(function\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*\(\)\s*\{?")]
GO_PATS = [re.compile(r"^func\s+(\((?P<recv>[^)]*)\)\s*)?(?P<name>[A-Za-z_]\w*)\s*[\[(]"),
           re.compile(r"^type\s+(?P<name>[A-Za-z_]\w*)\s+(struct|interface)\b")]
def go_kind(m, ln):
    if ln.startswith("type "): return "type"
    if m.groupdict().get("recv"):
        recv = m.group("recv").split()[-1].lstrip("*").split("[")[0]
        return ("method", recv)
    return "function"
PS_PATS = [re.compile(r"^\s*function\s+(?P<name>[A-Za-z_][\w-]*)", re.I)]

def main():
    allf = enclave_files() + deskbox_files()
    pending = []
    for path, text in allf:
        lg = lang(path, text)
        fid = path
        files.append({"id": fid, "comp": component(path), "lang": lg, "lines": text.count("\n") + 1,
                      "head": next((l.strip("#/ ").strip() for l in text.split("\n")[:12]
                                    if re.match(r"^\s*(#|//|\"\"\")\s*\w", l) and not l.startswith("#!")), "")[:300]})
        if lg == "python": pending.append((fid, "python", py(fid, text)))
        elif lg == "js": pending.append((fid, "js", regex_defs(fid, text, JS_PATS, js_kind)))
        elif lg == "html":
            pending.append((fid, "js", regex_defs(fid, text, JS_PATS, js_kind)))
        elif lg == "shell": pending.append((fid, "shell", regex_defs(fid, text, SH_PATS, lambda m, l: "shell function")))
        elif lg == "go": pending.append((fid, "go", regex_defs(fid, text, GO_PATS, go_kind)))
        elif lg == "powershell": pending.append((fid, "ps", regex_defs(fid, text, PS_PATS, lambda m, l: "function")))
    # resolve calls
    by_file, by_name = {}, {}
    for s in symbols:
        if s["kind"] in ("class", "type"):
            continue
        by_file.setdefault((s["file"], s["name"]), []).append(s["id"])
        by_name.setdefault((s["file"].startswith("deskbox/"), s["name"]), []).append(s["id"])
    seen = set()
    common = {"main", "run", "log", "get", "set", "put", "list", "load", "start", "stop", "close", "open", "init", "check",
              "fail", "pass", "skip", "send", "read", "write", "status", "add", "delete", "test", "setUp", "tearDown", "render",
              "update", "post", "call", "handle", "parse", "validate", "String", "Error", "New", "Run", "Close", "Get", "name", "key"}
    top = {}      # (file, name) -> top-level function/class ids
    meth = {}     # (file, class, name) -> ids
    fmeth = {}    # (file, name) -> method ids
    modfile = {}  # python module name -> file
    for s in symbols:
        if s["parent"] is None and s["kind"] in ("function", "class"): top.setdefault((s["file"], s["name"]), []).append(s["id"])
        if s["kind"] == "method": meth.setdefault((s["file"], s["parent"], s["name"]), []).append(s["id"]); fmeth.setdefault((s["file"], s["name"]), []).append(s["id"])
    for f in files:
        if f["lang"] == "python": modfile.setdefault(os.path.splitext(os.path.basename(f["id"]))[0].replace("-", "_"), f["id"])
    uniq_top = {}
    for (f, n), ids in top.items(): uniq_top.setdefault((f.startswith("deskbox/"), n), []).extend(ids)
    def link(a, ids):
        for t in ids or []:
            if t != a and (a, t) not in seen: seen.add((a, t)); calls.append([a, t])
    for fid, lg, entries in pending:
        dbx = fid.startswith("deskbox/")
        if lg == "python":
            al = MODALIAS.get(fid, {})
            for sid, refs in entries:
                for kind, name, base, cls in refs:
                    if kind in ("name", "ref"):
                        t = top.get((fid, name))
                        if not t and name in al and ":" in al[name]:
                            mod, attr = al[name].split(":"); mf = modfile.get(mod.split(".")[-1]); t = top.get((mf, attr)) if mf else None
                        if not t and kind == "name" and name not in common:
                            cand = uniq_top.get((dbx, name), []); t = cand if len(cand) == 1 else None
                        if t and kind == "ref" and any(i.split("#")[1].startswith(name + "@") is False for i in []): pass
                        link(sid, t)
                    elif kind in ("self", "selfref", "super"):
                        t = meth.get((fid, cls, name)) if kind != "super" else None
                        if not t and kind != "selfref" and name not in common and not name.startswith("__"):
                            c2 = fmeth.get((fid, name), []); t = c2 if len(c2) == 1 else None
                        link(sid, t)
                    elif kind == "attr":
                        t = None
                        if base and base in al:
                            mf = modfile.get(al[base].split(":")[-1].split(".")[-1]) if al[base] != "?" else None
                            t = top.get((mf, name)) if mf else None
                        if not t and base and base in modfile and base not in al:
                            t = top.get((modfile[base], name))
                        if not t and name not in common and not name.startswith("__"):
                            c2 = fmeth.get((fid, name), []) or top.get((fid, name), [])
                            t = c2 if len(c2) == 1 else None
                        link(sid, t)
            continue
        for sid, names in entries:
            for raw in names:
                weak = raw.startswith("word:")
                name = raw.split(":", 1)[1] if weak else raw
                if weak and lg != "shell": continue
                tgt = [i for i in (top.get((fid, name)) or []) + (fmeth.get((fid, name)) or [])] if lg != "go" else \
                      [s["id"] for s in symbols if s["file"].startswith(os.path.dirname(fid) + "/") and s["name"] == name and s["kind"] not in ("type",)]
                if lg == "shell":
                    tgt = [s["id"] for s in symbols if s["file"] == fid and s["name"] == name]
                link(sid, tgt)

    # tests: tag
    for s in symbols:
        s["test"] = bool(re.search(r"(^|/)test_|\.test\.|_test\.go$|smoke-test", s["file"])) or s["name"].startswith("test")
    json.dump({"files": files, "symbols": symbols, "calls": calls, "modcalls": MODCALLS,
               "meta": {"enclave": sh("git", "rev-parse", "--short", "HEAD", cwd=ENC).strip(),
                        "tools": sh("git", "rev-parse", "--short", "HEAD", cwd=os.path.join(ENC, "tools")).strip(),
                        "deskbox": DBX_REV}}, sys.stdout, separators=(",", ":"))

main()
