#!/usr/bin/env python3
"""What is reachable only from the dead entry points?

Builds a coarse call graph over the whole src tree: a symbol is a node, an
edge is "this function's body names that symbol" (by bare name, so it is
over- rather than under-connected -- safer for a deletion argument, since a
false edge keeps code alive rather than deleting it).

LIVE ROOTS: every endpoint a client reaches, module-level code, the launcher
entry, and anything a stack job names. Everything not reachable from a live
root is dead weight; reported per dead entry point as "exclusive" lines.
"""
import ast, json, re, sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1])
data = json.load(open(sys.argv[2]))
client = ""
for p in list(Path(sys.argv[3]).rglob("*.html")) + list(Path(sys.argv[4]).glob("*")):
    if p.is_file():
        client += "\n" + p.read_text(errors="replace")

# symbol table: bare name -> [(module, name, lines)]
sym = defaultdict(list)
for m in data:
    for s in m["symbols"]:
        sym[s["name"].split(".")[-1]].append((m["module"], s["name"], s["lines"]))

# edges: for every function, the names it mentions
edges = defaultdict(set)
node_lines = {}
for m in data:
    src = (root / m["path"]).read_text(errors="replace")
    tree = ast.parse(src)
    class V(ast.NodeVisitor):
        def __init__(self): self.stack = []
        def visit_FunctionDef(self, node):
            key = (m["module"], node.name)
            node_lines[key] = (node.end_lineno or node.lineno) - node.lineno + 1
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            for nm in names:
                for mod, full, _ in sym.get(nm, []):
                    edges[key].add((mod, full.split(".")[0]))
            self.generic_visit(node)
        visit_AsyncFunctionDef = visit_FunctionDef
    V().visit(tree)

def reached(path):
    parts = [p for p in re.split(r"\{[^}]*\}", path) if p not in ("", "/")]
    if not parts:
        return path in client
    return re.search(".{0,40}?".join(re.escape(p) for p in parts), client, re.S) is not None

live_roots, dead_eps = set(), []
for m in data:
    for e in m["endpoints"]:
        if reached(e["path"]):
            live_roots.add((m["module"], e["func"]))
        else:
            dead_eps.append((m["module"], e["func"], e["path"]))
# Infrastructure modules are live wholesale: they are entered from outside.
for m in data:
    if m["module"] in ("launcher", "db", "profile_manager", "paths", "clock", "config"):
        for s in m["symbols"]:
            live_roots.add((m["module"], s["name"].split(".")[0]))

def closure(roots):
    seen, stack = set(), list(roots)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(edges.get(n, ()))
    return seen

live = closure(live_roots)
print(f"live closure: {len(live)} symbols")
rows = []
for mod, func, path in dead_eps:
    excl = closure({(mod, func)}) - live
    lines = sum(node_lines.get(n, 0) for n in excl)
    rows.append((lines, len(excl), path, f"{mod}.{func}"))
print("\n== dead endpoints and what only they reach")
for lines, n, path, fn in sorted(rows, reverse=True):
    print(f"  {lines:>5} lines  {n:>3} symbols  {path:<46} {fn}")
print(f"\n  total exclusive to dead endpoints: {sum(r[0] for r in rows)} lines")

allsym = {(m["module"], s["name"].split(".")[0]) for m in data for s in m["symbols"]}
orphan = allsym - live - {(m, f) for _, _, _, mf in rows for m, f in [mf.rsplit(".", 1)]}
per_mod = defaultdict(int)
for mod, name in orphan:
    per_mod[mod] += node_lines.get((mod, name), 0)
print("\n== modules with the most code unreachable from any live root")
for mod, lines in sorted(per_mod.items(), key=lambda kv: -kv[1])[:15]:
    if lines:
        print(f"  {lines:>6} lines  {mod}")
