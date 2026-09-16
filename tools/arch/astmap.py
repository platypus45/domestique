#!/usr/bin/env python3
"""Measure a Python tree with the AST: symbols, references, endpoints, clones.

Everything here is counted, not guessed:
  symbols   every def/class with its line span and complexity (branch count)
  refs      every Name/Attribute use, so a symbol with no use outside itself
            is a deletion candidate
  imports   module-level AND function-local (this codebase imports inside
            functions to break cycles, so a module-level graph lies)
  endpoints @app.<method>("/path") with the function that serves it
  clones    function bodies normalised (names and constants erased) and
            hashed, so the same logic written twice shows up whatever it
            was called
Writes JSON to stdout or a file; ast.py itself stays dependency-free.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

BRANCH = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.IfExp,
          ast.comprehension, ast.ExceptHandler, ast.Assert, ast.Match)


def complexity(node: ast.AST) -> int:
    return 1 + sum(isinstance(n, BRANCH) for n in ast.walk(node))


def normalise(node: ast.AST) -> str:
    """A function body with every name and constant erased: same shape, same
    hash. Catches copy-paste that was renamed."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.append("N")
        elif isinstance(n, ast.Constant):
            out.append("C")
        elif isinstance(n, ast.Attribute):
            out.append("A." + n.attr)
        elif isinstance(n, ast.Call):
            out.append("Call")
        else:
            out.append(type(n).__name__)
    return hashlib.sha1(" ".join(out).encode()).hexdigest()[:16]


def walk_module(path: Path, root: Path) -> dict:
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    mod = str(path.relative_to(root)).removesuffix(".py").replace("/", ".")
    symbols, imports, endpoints, refs = [], [], [], set()

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _record(self, node, kind):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            deco = []
            for d in getattr(node, "decorator_list", []):
                target = d.func if isinstance(d, ast.Call) else d
                if isinstance(target, ast.Attribute):
                    deco.append(f"{getattr(target.value, 'id', '?')}.{target.attr}")
                    if (getattr(target.value, "id", "") == "app"
                            and isinstance(d, ast.Call) and d.args
                            and isinstance(d.args[0], ast.Constant)):
                        endpoints.append({"method": target.attr.upper(), "path": d.args[0].value,
                                          "func": node.name, "line": node.lineno})
                elif isinstance(target, ast.Name):
                    deco.append(target.id)
            symbols.append({"module": mod, "name": ".".join(self.stack + [node.name]), "kind": kind,
                            "line": node.lineno, "lines": span, "complexity": complexity(node),
                            "decorators": deco, "body_hash": normalise(node) if kind == "def" else "",
                            "args": len(getattr(getattr(node, "args", None), "args", []) or [])})

        def visit_FunctionDef(self, node):
            self._record(node, "def")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self._record(node, "class")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Import(self, node):
            for a in node.names:
                imports.append({"module": mod, "target": a.name.split(".")[0],
                                "local": bool(self.stack), "line": node.lineno})
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if node.module:
                imports.append({"module": mod, "target": node.module.split(".")[0],
                                "names": [a.name for a in node.names],
                                "local": bool(self.stack), "line": node.lineno})
            self.generic_visit(node)

        def visit_Name(self, node):
            refs.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            refs.add(node.attr)
            self.generic_visit(node)

    V().visit(tree)
    return {"module": mod, "path": str(path.relative_to(root)), "lines": src.count("\n") + 1,
            "symbols": symbols, "imports": imports, "endpoints": endpoints, "refs": sorted(refs)}


def main(argv):
    root = Path(argv[1]).resolve()
    out = [walk_module(p, root) for p in sorted(root.rglob("*.py")) if ".venv" not in p.parts]
    json.dump(out, open(argv[2], "w") if len(argv) > 2 else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
