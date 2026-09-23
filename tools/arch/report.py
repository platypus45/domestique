#!/usr/bin/env python3
"""Turn astmap's JSON into the numbers a slimming plan needs."""
import json, re, sys
from collections import defaultdict
from pathlib import Path

d = json.load(open(sys.argv[1]))
mods = {m["module"]: m for m in d}
DASH = Path(sys.argv[2]).read_text(errors="replace") if len(sys.argv) > 2 else ""
STACK = ""
for p in Path(sys.argv[3]).glob("*") if len(sys.argv) > 3 else []:
    if p.is_file():
        STACK += p.read_text(errors="replace")

print("== 12 biggest modules")
for m in sorted(d, key=lambda m: -m["lines"])[:12]:
    defs = [s for s in m["symbols"] if s["kind"] == "def"]
    print(f"  {m['lines']:>6}  {m['module']:<34} {len(defs):>4} defs  "
          f"max fn {max((s['lines'] for s in defs), default=0):>4}  "
          f"max cx {max((s['complexity'] for s in defs), default=0):>3}")

print("\n== 15 longest functions")
allfns = [s for m in d for s in m["symbols"] if s["kind"] == "def"]
for s in sorted(allfns, key=lambda s: -s["lines"])[:15]:
    print(f"  {s['lines']:>5} lines  cx {s['complexity']:>3}  {s['module']}:{s['line']}  {s['name']}")

print("\n== 10 most complex functions")
for s in sorted(allfns, key=lambda s: -s["complexity"])[:10]:
    print(f"  cx {s['complexity']:>3}  {s['lines']:>5} lines  {s['module']}:{s['line']}  {s['name']}")

# module usage: who references a module (imports, local or not)
importers = defaultdict(set)
for m in d:
    for i in m["imports"]:
        if i["target"] in mods:
            importers[i["target"]].add(m["module"])
print("\n== modules nothing imports (candidates; scripts/ excluded from blame)")
for mod, m in sorted(mods.items(), key=lambda kv: -kv[1]["lines"]):
    users = importers[mod] - {mod}
    if not users and not mod.startswith("scripts."):
        print(f"  {m['lines']:>6}  {mod}")

print("\n== modules used only by scripts/ or one caller")
for mod, m in sorted(mods.items(), key=lambda kv: -kv[1]["lines"]):
    users = importers[mod] - {mod}
    if users and (all(u.startswith("scripts.") for u in users) or len(users) == 1):
        print(f"  {m['lines']:>6}  {mod:<32} <- {', '.join(sorted(users))}")

# unused top-level symbols: defined once, never named anywhere else
refs_all = defaultdict(int)
for m in d:
    for r in m["refs"]:
        refs_all[r] += 1
print("\n== top-level defs never referenced anywhere (by name), 20 biggest")
cands = []
for m in d:
    for s in m["symbols"]:
        base = s["name"].split(".")[0]
        if "." in s["name"] or s["decorators"] or base.startswith("__"):
            continue
        hits = sum(1 for mm in d if base in mm["refs"])
        if hits == 0 and base not in DASH and base not in STACK:
            cands.append(s)
for s in sorted(cands, key=lambda s: -s["lines"])[:20]:
    print(f"  {s['lines']:>5} lines  {s['module']}:{s['line']}  {s['name']}")
print(f"  ({len(cands)} such defs, {sum(s['lines'] for s in cands)} lines total)")

# clones
by_hash = defaultdict(list)
for s in allfns:
    if s["lines"] >= 10 and s["body_hash"]:
        by_hash[s["body_hash"]].append(s)
print("\n== duplicate function bodies (same shape, names erased)")
dupes = [v for v in by_hash.values() if len(v) > 1]
tot = 0
for group in sorted(dupes, key=lambda g: -g[0]["lines"] * (len(g) - 1))[:12]:
    tot += group[0]["lines"] * (len(group) - 1)
    print(f"  {group[0]['lines']:>4} lines x{len(group)}: " +
          ", ".join(f"{s['module']}:{s['line']} {s['name']}" for s in group[:4]))
print(f"  ({len(dupes)} clone groups; {sum(g[0]['lines'] * (len(g)-1) for g in dupes)} duplicated lines)")

# endpoints vs clients
eps = [e for m in d for e in m["endpoints"]]
def used(path):
    stem = re.sub(r"\{[^}]+\}", "", path).rstrip("/")
    return stem and (stem in DASH or stem in STACK)
unused = [e for e in eps if not used(e["path"])]
print(f"\n== endpoints: {len(eps)} total, {len(unused)} referenced by neither the dashboard nor the stack")
for e in sorted(unused, key=lambda e: e["path"])[:40]:
    print(f"  {e['method']:<6} {e['path']:<52} {e['func']}")
if len(unused) > 40:
    print(f"  ... and {len(unused)-40} more")
