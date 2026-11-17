"""Tree view for an Atlas page: the repo drawn top-down (repo -> areas -> components -> files -> functions).

    python3 treeview.py IN.html OUT.html     add it to a built Atlas page (replaces an earlier copy)
    from treeview import add_tree            same, from an Atlas build.py

tree.js plugs into the Atlas engine (nodes, links, select, setView); tree.css styles it. Works with both
the three.js engine (Layers/Linear) and the older 3d-force-graph one. index.html#tree opens straight into it.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))

def add_tree(page):
    page = re.sub(r"\n?<!--TREE-->.*?<!--/TREE-->\n?", "\n", page, flags=re.S)
    css = open(os.path.join(HERE, "tree.css")).read()
    js = open(os.path.join(HERE, "tree.js")).read().replace("</", "<\\/")
    i = page.rindex("</script>") + len("</script>")      # after the engine
    page = page[:i] + f"\n<!--TREE--><style>\n{css}</style>\n<script>\n{js}</script><!--/TREE-->\n" + page[i:]
    # the engine clears the URL hash on start, so note it before the engine runs
    j = page.index("<script")
    return page[:j] + "<!--TREE--><script>var ATLAS_START_HASH = location.hash;</script><!--/TREE-->\n" + page[j:]

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    page = add_tree(open(src, encoding="utf-8").read())
    open(dst, "w", encoding="utf-8").write(page)
    print("wrote", dst, len(page) // 1024, "KiB")
