#!/usr/bin/env python3
"""Which endpoints does any client actually reach?

Clients: every template (the dashboard and the setup pages), and the
cycling-stack jobs. A path with parameters is matched on the literal part
before the first "{", so /api/ride/{id}/prs counts as reached only if
"/api/ride/" appears with "prs" nearby -- checked by regex over the client
text, not by stem equality.
"""
import json, re, sys
from pathlib import Path

data = json.load(open(sys.argv[1]))
client = ""
for p in list(Path(sys.argv[2]).rglob("*.html")) + list(Path(sys.argv[3]).glob("*")):
    if p.is_file():
        client += "\n" + p.read_text(errors="replace")

eps = [e for m in data for e in m["endpoints"]]
rows = []
for e in eps:
    parts = [p for p in re.split(r"\{[^}]*\}", e["path"]) if p not in ("", "/")]
    if not parts:
        hit = e["path"] in client
    else:
        pat = ".{0,40}?".join(re.escape(p) for p in parts)
        hit = re.search(pat, client, re.S) is not None
    rows.append((hit, e))

unused = [e for hit, e in rows if not hit]
print(f"{len(eps)} endpoints, {len(unused)} unreferenced by any template or stack job\n")
for e in sorted(unused, key=lambda e: e["path"]):
    print(f"  {e['method']:<6} {e['path']:<50} {e['func']}")
