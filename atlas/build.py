"""Build docs/atlas/index.html: extract the graph, enrich it, and assemble template.html + engine.js + runtime.json.

    python3 atlas/build.py            (from the repo root; needs a deskbox clone beside this repo, or DESKBOX_REPO)
"""
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
raw = subprocess.run([sys.executable, "-I", "extract.py"], capture_output=True, text=True, check=True).stdout
open("graph.raw.json", "w").write(raw)
graph = subprocess.run([sys.executable, "-I", "enrich.py", "graph.raw.json"], capture_output=True, text=True, check=True).stdout
os.remove("graph.raw.json")
import json
t = open("template.html").read()
head = t[:t.index('<script src="https://cdn.jsdelivr.net/npm/3d-force-graph')]
def sub(old, new):
    global head
    assert old in head, old[:80]
    head = head.replace(old, new, 1)
sub('<meta name="claude:viewport-fit-sides" content="cover">\n', '')   # artifact-only hint
sub('''        <button data-view="all" aria-pressed="true">Everything</button>
        <button data-view="runtime" aria-pressed="false">Runtime</button>
        <button data-view="findings" aria-pressed="false">Findings</button>''',
'''        <button data-view="explore" aria-pressed="true">Layers</button>
        <button data-view="all" aria-pressed="false">Everything</button>
        <button data-view="findings" aria-pressed="false">Findings</button>''')
sub('      <div class="shape-note" id="viewNote"></div>',
    '      <div class="views" style="grid-template-columns:1fr 1fr"><button id="flat" aria-pressed="false">Flatten to top-down</button><button id="collapse" aria-pressed="false">Collapse into files</button></div>\n'
    '      <div class="shape-note" id="viewNote"></div>')
sub('.lbl { position: absolute; transform: translate(-50%, -140%);', '.lbl { position: absolute; left: 0; top: 0; will-change: transform; transform: translate(-50%, -140%);')
sub('<div id="graph" aria-label="3D map of the Enclave project"></div>',
    '<div id="graph" aria-label="3D map of the Enclave project, explored in layers"></div>')
sub('<aside id="drawer" class="panel" hidden>', '<nav id="crumbs" class="panel" hidden aria-label="Layer trail"></nav>\n<aside id="drawer" class="panel" hidden>')
sub('@media (max-width: 760px) {', '''.lbl.file { font: 500 12px/1.2 var(--f-mono); }
.lbl.sym { font: 400 11.5px/1.2 var(--f-mono); color: #c9d1dd; border-color: #2a364899; }
.lbl.focus { color: var(--accent); border-color: var(--accent); font-size: 14px; font-weight: 600; z-index: 1; }
#rail, #drawer { max-height: calc(100% - 96px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)); }
#crumbs { left: max(12px, env(safe-area-inset-left, 0px)); right: max(12px, env(safe-area-inset-right, 0px));
  bottom: calc(12px + env(safe-area-inset-bottom, 0px)); margin-inline: auto; max-width: 900px; padding: 8px 10px;
  display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: center; }
#crumbs .back { all: unset; cursor: pointer; grid-row: span 2; width: 34px; height: 34px; border-radius: 4px; border: 1px solid var(--line);
  display: grid; place-items: center; font-size: 16px; color: var(--fg); }
#crumbs .back:disabled { opacity: .3; cursor: default; }
#crumbs .back:not(:disabled):hover { border-color: var(--accent); color: var(--accent); }
#crumbs .trail { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px; min-width: 0; font: 500 13px var(--f-mono); }
#crumbs .trail button { all: unset; cursor: pointer; color: var(--mut); border-bottom: 1px dotted var(--line); }
#crumbs .trail button:hover, #crumbs .trail button:focus-visible { color: var(--fg); border-bottom-color: var(--accent); }
#crumbs .here { color: var(--accent); font-weight: 600; word-break: break-word; }
#crumbs .sep { color: var(--line); }
#crumbs .hint { font-size: 11.5px; color: var(--mut); min-width: 0; }
#crumbs .hint b { color: var(--fg); font-variant-numeric: tabular-nums; }
@media (max-width: 760px) {
  #drawer { bottom: calc(96px + env(safe-area-inset-bottom, 0px)) !important; max-height: 38% !important; }
  #crumbs .hint { display: none; }''')
libs = "".join(f'<script src="https://cdn.jsdelivr.net/npm/{u}"></script>\n' for u in [
    "three@0.147.0/build/three.min.js", "three@0.147.0/examples/js/controls/OrbitControls.js",
    "d3-dispatch@3.0.1/dist/d3-dispatch.min.js", "d3-quadtree@3.0.1/dist/d3-quadtree.min.js", "d3-timer@3.0.1/dist/d3-timer.min.js",
    "d3-binarytree@1.0.2/dist/d3-binarytree.min.js", "d3-octree@1.0.2/dist/d3-octree.min.js", "d3-force-3d@3.0.5/dist/d3-force-3d.min.js"])
eng = open("engine.js").read().replace("/*DATA*/null", graph, 1) \
    .replace("/*RUNTIME*/null", json.dumps(json.load(open("runtime.json")), separators=(",", ":")), 1)
# A standalone page: the document skeleton, and the small reset the page assumes (hidden means hidden, no body margin).
i = head.index("</style>") + len("</style>")
page = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        "<style>body{margin:0}[hidden]{display:none!important}</style>\n" + head[:i] + "\n</head>\n<body>\n" + head[i:]
        + libs + "<script>\n" + eng + "\n</script>\n</body>\n</html>\n")
out = os.path.join(HERE, "..", "docs", "atlas", "index.html")
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w").write(page)
print("wrote", os.path.relpath(out), len(page) // 1024, "KiB")
