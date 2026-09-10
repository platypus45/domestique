#!/usr/bin/env python3
"""Measure the codebase's shape instead of arguing about it.

Every finding here is derived from the AST, so it can be re-run after a change
and the number either moved or it did not. It reports on the things that make a
codebase hard to change safely:

  size        which functions are too big to hold in your head
  arity       parameter lists long enough that call sites drift out of step
  dead        unreachable statements, and module-level defs nothing calls
  layers      functions that mix HTTP, SQL, filesystem and domain logic
  duplication same-shaped function bodies -- one rule implemented N times
  state       module-level mutable globals and who writes them
  coupling    imports in, imports out

Usage:
    scripts/architecture_report.py [--section size|arity|dead|layers|duplication|state|coupling]
    scripts/architecture_report.py --json      machine-readable, for diffing runs
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# Thresholds. Not laws -- the point is a stable line so two runs are comparable.
LONG_FN = 60           # lines; beyond this a function stops fitting on a screen
HUGE_FN = 200          # lines; beyond this it is a module wearing a def
MANY_ARGS = 8          # parameters
DEEP_NEST = 5          # nesting depth
BIG_MODULE = 2000      # lines


def modules():
    for f in sorted(SRC.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        try:
            yield f, ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue


def fn_lines(node) -> int:
    return (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1


def depth(node, d=0) -> int:
    best = d
    for child in ast.iter_child_nodes(node):
        nd = d + 1 if isinstance(child, (ast.If, ast.For, ast.While, ast.With,
                                         ast.Try, ast.AsyncFor, ast.AsyncWith)) else d
        best = max(best, depth(child, nd))
    return best


def branches(node) -> int:
    return sum(isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp,
                              ast.IfExp, ast.ExceptHandler, ast.Assert))
               for n in ast.walk(node))


def all_functions():
    for path, tree in modules():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield path, node


# ── size ─────────────────────────────────────────────────────────────────
def section_size():
    rows = []
    for path, fn in all_functions():
        n = fn_lines(fn)
        if n >= LONG_FN:
            rows.append((n, branches(fn), depth(fn), path.name, fn.name, fn.lineno))
    rows.sort(reverse=True)
    total = sum(r[0] for r in rows)
    print(f"\n{'='*78}\nFUNCTIONS OVER {LONG_FN} LINES\n{'='*78}")
    print(f"{len(rows)} of them, {total:,} lines -- "
          f"{sum(1 for r in rows if r[0] >= HUGE_FN)} are over {HUGE_FN}\n")
    print(f"{'lines':>6}{'branch':>7}{'nest':>6}  {'module':<26}{'function'}")
    for n, b, d, mod, name, ln in rows[:30]:
        print(f"{n:>6}{b:>7}{d:>6}  {mod:<26}{name}  :{ln}")
    if len(rows) > 30:
        print(f"       ... and {len(rows)-30} more")
    return [{"lines": n, "branches": b, "nesting": d, "module": m,
             "function": f, "line": l} for n, b, d, m, f, l in rows]


# ── arity ────────────────────────────────────────────────────────────────
def section_arity():
    rows = []
    for path, fn in all_functions():
        a = fn.args
        n = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
        if n >= MANY_ARGS:
            rows.append((n, path.name, fn.name, fn.lineno))
    rows.sort(reverse=True)
    print(f"\n{'='*78}\nFUNCTIONS TAKING {MANY_ARGS}+ PARAMETERS\n{'='*78}")
    print("A long parameter list is state that should have been an object: every\n"
          "call site has to reconstruct it, and they drift.\n")
    print(f"{'args':>5}  {'module':<26}{'function'}")
    for n, mod, name, ln in rows[:20]:
        print(f"{n:>5}  {mod:<26}{name}  :{ln}")
    if len(rows) > 20:
        print(f"      ... and {len(rows)-20} more")
    return [{"args": n, "module": m, "function": f, "line": l} for n, m, f, l in rows]


# ── dead ─────────────────────────────────────────────────────────────────
def section_dead():
    unreachable = []
    for path, tree in modules():
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for i, st in enumerate(body[:-1]):
                if isinstance(st, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    rest = body[i + 1:]
                    a = rest[0].lineno
                    b = max(getattr(x, "end_lineno", x.lineno) for x in rest)
                    unreachable.append((b - a + 1, path.name, a, b,
                                        type(st).__name__.lower()))
                    break
    unreachable.sort(reverse=True)

    # Module-level defs nothing references, anywhere in the tree.
    defined, referenced = {}, set()
    for path, tree in modules():
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("__") or node.name in ("main",):
                continue
            # A decorated function is registered by its decorator, not called
            # by name -- every Flask route in app.py looks unreferenced
            # otherwise. The first version of this check reported 162 dead
            # functions, almost all of them live HTTP endpoints.
            if node.decorator_list:
                continue
            defined[(path.name, node.name)] = node.lineno
    for f in list(SRC.rglob("*.py")) + list((ROOT / "tests").rglob("*.py")) \
            + list((ROOT / "scripts").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                # `from routes_lib import _route_key_for` is a reference. Missing
                # these reported imported-and-used helpers as dead.
                for a in node.names:
                    referenced.add(a.asname or a.name.split(".")[0])
                    referenced.add(a.name.split(".")[-1])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value)     # getattr("name") / route strings
    orphans = sorted((m, n, ln) for (m, n), ln in defined.items() if n not in referenced)

    print(f"\n{'='*78}\nDEAD CODE\n{'='*78}")
    print(f"unreachable statement blocks: {len(unreachable)}"
          f"  ({sum(u[0] for u in unreachable)} lines)")
    for n, mod, a, b, why in unreachable[:12]:
        print(f"   {n:>4} lines  {mod}:{a}-{b}   after {why}")
    print(f"\nmodule-level functions with no reference anywhere: {len(orphans)}")
    for mod, name, ln in orphans[:20]:
        print(f"   {mod}:{ln}  {name}")
    if len(orphans) > 20:
        print(f"   ... and {len(orphans)-20} more")
    return {"unreachable": [{"lines": n, "module": m, "start": a, "end": b, "after": w}
                            for n, m, a, b, w in unreachable],
            "orphans": [{"module": m, "function": n, "line": l} for m, n, l in orphans]}


# ── layers ───────────────────────────────────────────────────────────────
LAYER_MARKERS = {
    "http": ("route", "jsonify", "request", "abort", "redirect", "render_template",
             "Response", "make_response"),
    "sql": ("execute", "executemany", "cursor", "commit", "fetchall", "fetchone",
            "connect"),
    "fs": ("open", "write_text", "read_text", "mkdir", "unlink", "rmtree", "glob",
           "rglob"),
    # NOT "get"/"post"/"request": every dict .get() and every Flask request
    # object matched those, which put "net" on almost every handler in the
    # report and made the column meaningless. Real network calls are reached
    # through the modules in LAYER_MODULES or these verbs.
    "net": ("urlopen", "urlretrieve"),
}


# Calls through these modules are that layer even when the verb is domain-y:
# `db.query_activities()` is SQL however it is spelled.
LAYER_MODULES = {"db": "sql", "sqlite3": "sql", "requests": "net",
                 "urllib": "net", "shutil": "fs", "os": "fs", "pathlib": "fs"}


def layers_of(fn):
    found = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id in LAYER_MODULES:
                found.add(LAYER_MODULES[f.value.id])
            name = f.attr
        else:
            name = getattr(f, "id", None)
        if not name:
            continue
        for layer, marks in LAYER_MARKERS.items():
            if name in marks:
                found.add(layer)
    for d in getattr(fn, "decorator_list", []):
        if _ROUTE_DECOR.search(ast.unparse(d)):
            found.add("http")
    return found


_ROUTE_DECOR = re.compile(r"\.(route|get|post|put|patch|delete)\(")


def is_route(fn) -> bool:
    """Flask registers routes as @app.route AND as @app.get/@app.post/...

    Matching only ".route(" found zero handlers in a 20k-line web app, which
    is the kind of clean result worth distrusting.
    """
    return any(_ROUTE_DECOR.search(ast.unparse(d))
               for d in getattr(fn, "decorator_list", []))


def section_routes():
    """How much logic lives inside HTTP handlers.

    A handler's job is to parse the request, call one domain function, and
    serialise the answer. Everything past that is domain logic that cannot be
    reached without an HTTP request, cannot be reused by the CLI or the
    scheduler, and has to be tested through a test client.
    """
    rows = []
    for path, fn in all_functions():
        if not is_route(fn):
            continue
        rows.append((fn_lines(fn), branches(fn), sorted(layers_of(fn) - {"http"}),
                     path.name, fn.name, fn.lineno))
    rows.sort(reverse=True)
    thin = sum(1 for r in rows if r[0] <= 40)
    print(f"\n{'='*78}\nLOGIC INSIDE HTTP HANDLERS\n{'='*78}")
    print(f"{len(rows)} route handlers. {thin} are 40 lines or fewer; "
          f"{len(rows)-thin} are not.")
    print(f"{sum(r[0] for r in rows):,} lines sit behind a URL.\n")
    print(f"{'lines':>6}{'branch':>7}  {'also touches':<18}{'module':<12}{'handler'}")
    for n, b, ls, mod, name, ln in rows[:25]:
        print(f"{n:>6}{b:>7}  {'+'.join(ls) or '-':<18}{mod:<12}{name}  :{ln}")
    if len(rows) > 25:
        print(f"       ... and {len(rows)-25} more")
    return [{"lines": n, "branches": b, "layers": ls, "module": m,
             "handler": f, "line": l} for n, b, ls, m, f, l in rows]


def section_layers():
    rows = []
    for path, fn in all_functions():
        ls = layers_of(fn)
        if len(ls) >= 3 or ("http" in ls and "sql" in ls):
            rows.append((len(ls), sorted(ls), fn_lines(fn), path.name, fn.name, fn.lineno))
    rows.sort(key=lambda r: (-r[0], -r[2]))
    print(f"\n{'='*78}\nFUNCTIONS SPANNING MULTIPLE LAYERS\n{'='*78}")
    print("A request handler that also talks SQL and the filesystem cannot be\n"
          "tested without all three. These are the seams a decomposition follows.\n")
    print(f"{'layers':<26}{'lines':>6}  {'module':<24}{'function'}")
    for _n, ls, n_lines, mod, name, ln in rows[:25]:
        print(f"{'+'.join(ls):<26}{n_lines:>6}  {mod:<24}{name}  :{ln}")
    if len(rows) > 25:
        print(f"   ... and {len(rows)-25} more")
    print(f"\n{len(rows)} functions mix three or more layers "
          f"(or HTTP with SQL directly)")
    return [{"layers": ls, "lines": n, "module": m, "function": f, "line": l}
            for _c, ls, n, m, f, l in rows]


# ── duplication ──────────────────────────────────────────────────────────
def shape(fn) -> str:
    """Structural fingerprint: node types only, names and constants dropped."""
    parts = []
    for node in ast.walk(fn):
        parts.append(type(node).__name__)
    return hashlib.sha1(",".join(parts).encode()).hexdigest()


def section_duplication():
    buckets = collections.defaultdict(list)
    for path, fn in all_functions():
        if fn_lines(fn) < 15:
            continue
        buckets[shape(fn)].append((path.name, fn.name, fn.lineno, fn_lines(fn)))
    dupes = {k: v for k, v in buckets.items() if len(v) > 1}
    ranked = sorted(dupes.values(), key=lambda v: -(len(v) * v[0][3]))
    print(f"\n{'='*78}\nSTRUCTURALLY IDENTICAL FUNCTION BODIES\n{'='*78}")
    print("Same AST shape, different names. Each group is one rule implemented\n"
          "more than once -- fix it in one and the others stay wrong.\n")
    for group in ranked[:15]:
        print(f"   {len(group)}x ~{group[0][3]} lines:")
        for mod, name, ln, _n in group:
            print(f"        {mod}:{ln}  {name}")
    print(f"\n{len(dupes)} duplicate groups, "
          f"{sum(len(v)-1 for v in dupes.values())} redundant copies")
    return [[{"module": m, "function": f, "line": l, "lines": n} for m, f, l, n in g]
            for g in ranked]


# ── state ────────────────────────────────────────────────────────────────
def section_state():
    globals_by_mod = {}
    writers = collections.defaultdict(set)
    for path, tree in modules():
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id.isupper():
                    names.add(node.target.id)
        globals_by_mod[path.name] = names
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                for nm in node.names:
                    enclosing = "?"
                    for f in ast.walk(tree):
                        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                                and f.lineno <= node.lineno <= getattr(f, "end_lineno", f.lineno):
                            enclosing = f.name
                    writers[(path.name, nm)].add(enclosing)
    print(f"\n{'='*78}\nMUTABLE MODULE STATE\n{'='*78}")
    print("A global rebound from more than one function is state with no owner.\n")
    multi = {k: v for k, v in writers.items() if len(v) > 1}
    for (mod, nm), fns in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:18]:
        print(f"   {mod}:{nm}  rebound by {len(fns)}: {', '.join(sorted(fns))[:70]}")
    print(f"\n{len(writers)} globals are rebound at runtime; "
          f"{len(multi)} by more than one function")
    return {"rebound": {f"{m}:{n}": sorted(f) for (m, n), f in writers.items()}}


# ── coupling ─────────────────────────────────────────────────────────────
def section_coupling():
    local = {p.stem for p in SRC.rglob("*.py")}
    out = collections.defaultdict(set)
    for path, tree in modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in local:
                        out[path.stem].add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                base = node.module.split(".")[0]
                if base in local:
                    out[path.stem].add(base)
    for k in out:
        out[k].discard(k)
    fan_in = collections.Counter()
    for src_mod, deps in out.items():
        for d in deps:
            fan_in[d] += 1
    print(f"\n{'='*78}\nMODULE COUPLING\n{'='*78}")
    print(f"{'module':<26}{'imports':>9}{'imported by':>13}   size")
    rows = sorted(local, key=lambda m: -(len(out.get(m, ())) + fan_in[m]))
    for m in rows[:20]:
        f = SRC / f"{m}.py"
        n = len(f.read_text(encoding='utf-8').splitlines()) if f.exists() else 0
        flag = "  <-- big" if n >= BIG_MODULE else ""
        print(f"{m:<26}{len(out.get(m, ())):>9}{fan_in[m]:>13}{n:>8}{flag}")
    cycles = [(a, b) for a in out for b in out[a] if a in out.get(b, ())]
    print(f"\nimport cycles: {len(cycles)//2}")
    for a, b in sorted({tuple(sorted(c)) for c in cycles}):
        print(f"   {a} <-> {b}")
    return {"out": {k: sorted(v) for k, v in out.items()},
            "cycles": sorted({tuple(sorted(c)) for c in cycles})}


SECTIONS = {
    "size": section_size, "arity": section_arity, "dead": section_dead,
    "layers": section_layers, "duplication": section_duplication,
    "state": section_state, "coupling": section_coupling,
    "routes": section_routes,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", choices=sorted(SECTIONS))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    picked = [a.section] if a.section else list(SECTIONS)
    if a.json:
        import io
        import contextlib
        out = {}
        for s in picked:
            with contextlib.redirect_stdout(io.StringIO()):
                out[s] = SECTIONS[s]()
        json.dump(out, sys.stdout, indent=1, default=list)
        return
    for s in picked:
        SECTIONS[s]()


if __name__ == "__main__":
    main()
