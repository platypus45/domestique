#!/usr/bin/env python3
"""Which endpoints does a client actually reach?

    python3 tools/arch/endpoints.py <astmap.json> <client dir> [more dirs...]

WHY THIS IS NOT A GREP. The first version of this tool matched each route's
literal parts against the client text with a short wildcard between them, and
it was wrong three times out of twenty: the dashboard builds

    `/api/course/${encodeURIComponent(region)}/${encodeURIComponent(name)}/download`

and 24 characters of `${encodeURIComponent(` blew past the gap, so a live
endpoint was reported dead. Deleting on that evidence would have broken the
course download.

So this reads URLs, not text. Every string literal and template literal in the
client sources is parsed, `${...}` (and `' + x + '` concatenation) collapses to
a wildcard segment, the query and fragment are dropped, and a route matches a
client URL when their SEGMENTS line up: literal equals literal, and a route
parameter (`{ride_id}`) accepts any single segment, wildcard or not.

Anything the clients do not reach is then searched for across the whole repo --
tests, packaging, docs, the stack jobs -- and reported with those references,
because "no client calls it" and "nothing anywhere mentions it" are different
findings and only the second is a deletion argument on its own.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

WILDCARD = "\x00*"          # one path segment, contents unknown

# A URL-shaped string: quoted, starting with "/" (a path) and not a bare "/".
_STR = re.compile(r"""(['"`])(/[^'"`\n]{1,200}?)\1""")
# ${...} in a template literal, and ' + expr + ' concatenation.
_INTERP = re.compile(
    r"\$\{[^}]*\}"                                 # `${...}` in a template literal
    r"|['\"]\s*\+\s*[^+'\"\n]{1,100}?\s*\+\s*['\"]"   # '...' + expr + '...'
)


def client_urls(paths: list[Path]) -> set[str]:
    """Every URL path a client source mentions, with interpolation blanked."""
    out: set[str] = set()
    for root in paths:
        files = [root] if root.is_file() else [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix in (".html", ".js", ".py", ".sh", "")
        ]
        for p in files:
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            # Collapse interpolation BEFORE pulling strings out, or
            # `'/api/ride/' + id + '/detail'` is read as the bare prefix
            # `/api/ride/` and every route under it looks unreached.
            text = _INTERP.sub(WILDCARD, text)
            for _q, raw in _STR.findall(text):
                url = raw.split("?", 1)[0].split("#", 1)[0]
                if url.endswith("/"):
                    # `'/api/download/zwo/' + encodeURIComponent(f)` -- one
                    # concatenation with nothing after it. A trailing slash
                    # means a segment follows that the source does not spell.
                    url += WILDCARD
                if url.startswith("/"):
                    out.add(url)
    return out


def segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s != ""]


def reached(route: str, urls: set[str]) -> str:
    """"exact", "ambiguous" or "no".

    "ambiguous" means the only client URLs that line up do so through an
    interpolated segment -- `/api/routes/${id}` lines up with
    /api/routes/surfaces, and nothing in the source can say whether that id is
    ever the string "surfaces". Reported apart from the proven matches,
    because a maybe is not evidence either way and the difference is the
    whole reason this tool exists.
    """
    r = segments(route)
    verdict = "no"
    for url in urls:
        u = segments(url)
        if len(u) != len(r):
            continue
        guessed = False
        for rs, us in zip(r, u):
            if rs.startswith("{") and rs.endswith("}"):
                continue                       # a parameter takes any segment
            if WILDCARD in us:
                guessed = True                 # interpolated: could be anything
                continue
            if rs != us:
                break
        else:
            if not guessed:
                return "exact"
            verdict = "ambiguous"
    return verdict


def references(route: str, repo: Path, skip: tuple[str, ...]) -> list[str]:
    """Files anywhere in the repo that mention the route's literal prefix."""
    prefix = "/".join([""] + [s for s in segments(route)
                              if not s.startswith("{")][:3])
    if not prefix or prefix == "/":
        return []
    try:
        out = subprocess.run(
            ["git", "grep", "-l", "-F", "--", prefix], cwd=repo,
            capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    return [f for f in out if not any(s in f for s in skip)]


def main() -> int:
    data = json.load(open(sys.argv[1]))
    urls = client_urls([Path(p) for p in sys.argv[2:]])
    repo = Path(__file__).resolve().parents[2]
    eps = [e for m in data for e in m["endpoints"]
           if e["method"] != "MIDDLEWARE" and e["path"].startswith("/api")]
    by_verdict = {"exact": [], "ambiguous": [], "no": []}
    for e in eps:
        by_verdict[reached(e["path"], urls)].append(e)
    unreached = by_verdict["no"]

    print(f"{len(eps)} /api endpoints; {len(urls)} distinct client URLs; "
          f"{len(by_verdict['exact'])} reached, "
          f"{len(by_verdict['ambiguous'])} ambiguous, {len(unreached)} unreached\n")
    if by_verdict["ambiguous"]:
        print("  AMBIGUOUS -- matched only through an interpolated segment, "
              "so neither list is safe without reading the call site:")
        for e in sorted(by_verdict["ambiguous"], key=lambda e: e["path"]):
            print(f"    {e['method']:<6} {e['path']:<46} {e['func']}")
        print()
    print("  UNREACHED by any client:")
    for e in sorted(unreached, key=lambda e: e["path"]):
        refs = references(e["path"], repo,
                          skip=("src/app.py", ".library_index.json",
                                "characterization.json"))
        tests = [f for f in refs if f.startswith("tests/")]
        other = [f for f in refs if not f.startswith("tests/")]
        print(f"  {e['method']:<6} {e['path']:<48} {e['func']}")
        if other:
            print(f"         also named by: {', '.join(other[:4])}")
        if tests:
            print(f"         tests: {len(tests)} file(s)")
        if not refs:
            print("         nothing else in the repo names it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
