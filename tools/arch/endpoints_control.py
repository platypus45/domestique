#!/usr/bin/env python3
"""Negative control for endpoints.py: does it ever call a USED endpoint dead?

    python3 tools/arch/astmap.py src > /tmp/src.json
    python3 tools/arch/endpoints_control.py /tmp/src.json

Independently of the matcher, pull every path that appears inside a fetch() /
XHR open() call in the dashboard and the static JS, and require endpoints.py to
report the route it hits as reached. A matcher that cannot pass this has no
business producing a deletion list -- the first version failed it three times,
once on a URL built with encodeURIComponent and twice on `'/api/x/' + id`.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import endpoints as ep                                            # noqa: E402

FETCH = re.compile(r"""(?:fetch|open|\.get|\.post)\s*\(\s*(['"`])([^'"`\n]{1,200}?)\1""")
CLIENTS = ("src/templates", "src/static")


def main() -> int:
    data = json.load(open(sys.argv[1]))
    routes = [e for m in data for e in m["endpoints"]
              if e["method"] != "MIDDLEWARE" and e["path"].startswith("/api")]
    urls = ep.client_urls([pathlib.Path(c) for c in CLIENTS])

    called = set()
    for root in CLIENTS:
        for p in pathlib.Path(root).rglob("*"):
            if not p.is_file() or p.suffix not in (".html", ".js"):
                continue
            text = ep._INTERP.sub(ep.WILDCARD, p.read_text(errors="replace"))
            for _q, raw in FETCH.findall(text):
                u = raw.split("?")[0].split("#")[0]
                if u.endswith("/"):
                    u += ep.WILDCARD
                if u.startswith("/api"):
                    called.add(u)

    bad = []
    for u in sorted(called):
        segs = ep.segments(u)
        hit = None
        for e in routes:
            r = ep.segments(e["path"])
            if len(r) != len(segs):
                continue
            if all(rs.startswith("{") or ep.WILDCARD in us or rs == us
                   for rs, us in zip(r, segs)):
                hit = e
                if ep.reached(e["path"], urls) == "exact":
                    break
        if hit is None:
            bad.append((u, "no route matches this call at all"))
        elif ep.reached(hit["path"], urls) == "no":
            bad.append((u, f"called, but {hit['path']} is reported UNREACHED"))

    print(f"{len(called)} distinct /api paths inside a fetch() in the UI; "
          f"misidentified: {len(bad)}")
    for u, why in bad:
        print("   ", u.replace(ep.WILDCARD, "${}"), "->", why)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
