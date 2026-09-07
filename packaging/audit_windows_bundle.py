"""Provenance audit of a shipped Domestique Windows bundle.

Proves every byte of executable content in the bundle comes from a public,
verifiable source: every embedded Python module is recompiled from its
source (this repo at the release tag, CPython's Lib, the exact PyPI wheel,
PyInstaller) and compared as a code object; every DLL/pyd is matched by
SHA-256 against the PyPI wheel or python.org's own Windows build; the
bootloader is compared section by section with PyInstaller's stock one.
Written for the 2026-09 Windows Defender report (docs/security/).

Usage (from an unzipped release Domestique-Windows.zip, with the build venv):
  cd <scratch dir containing unz/ (the unzipped bundle)>
  .venv-build/bin/python packaging/audit_windows_bundle.py <repo root> \
      <installed-versions.txt from the CI "Successfully installed" line>
Needs: gh (for nothing here), pip, network to PyPI + python.org.
Companion step (Authenticode on the remaining files): osslsigncode verify
with Microsoft's roots, see the report.
"""
import sys, os, json, marshal, hashlib, subprocess, zipfile, urllib.request, struct, types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
W = sys.argv[1]; ROOT = Path.cwd(); EXE = ROOT / "unz/Domestique.exe"; INTERNAL = ROOT / "unz/_internal"
PYV = "3.12.10"
log = open("verify_report.txt", "w")
def P(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); log.write(s + "\n"); log.flush()
sha = lambda b: hashlib.sha256(b).hexdigest()

# ── 1. exact wheels, verified against PyPI digests ───────────────────────────
versions = sorted({l.strip() for l in open(sys.argv[2] if len(sys.argv) > 2 else "installed_3.11.4.txt") if l.strip()})
WD = ROOT / "wheels"; WD.mkdir(exist_ok=True); WX = ROOT / "wheels_x"; WX.mkdir(exist_ok=True)
def norm(n): return n.lower().replace("-", "_").replace(".", "_")
def matches(p, name, ver): return norm(p.name).startswith(norm(name) + "_" + norm(ver) + "_")
wheel_status = {}
for nv in versions:
    name, ver = nv.rsplit("-", 1)
    have = [p for p in list(WD.glob("*.whl")) + list(WD.glob("*.tar.gz")) if matches(p, name, ver)]
    if not have:
        r = subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--only-binary=:all:",
                            "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
                            "--abi", "cp312", "--abi", "abi3", "--abi", "none", f"{name}=={ver}", "-d", str(WD)],
                           capture_output=True, text=True)
        have = [p for p in WD.glob("*.whl") if matches(p, name, ver)]
        if not have:  # pure-python sdist only (proxy_tools)
            subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--no-binary=:all:", f"{name}=={ver}", "-d", str(WD)], capture_output=True, text=True)
            have = [p for p in WD.glob("*.tar.gz") if matches(p, name, ver)]
        if not have:
            wheel_status[nv] = "DOWNLOAD FAILED"; continue
    whl = have[0]
    try:
        js = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30))
        digests = {f["digests"]["sha256"]: f["filename"] for f in js["urls"]}
    except Exception as e:
        wheel_status[nv] = f"PYPI LOOKUP FAILED: {e}"; continue
    h = sha(whl.read_bytes())
    wheel_status[nv] = "sha256 == PyPI" if h in digests else f"SHA MISMATCH vs PyPI ({whl.name})"
    dest = WX / norm(name)
    if not dest.exists():
        if whl.suffix == ".whl":
            zipfile.ZipFile(whl).extractall(dest)
        else:
            import tarfile
            tarfile.open(whl).extractall(dest)
            inner = [d for d in dest.iterdir() if d.is_dir()]
            if len(inner) == 1:  # sdist: <name>-<ver>/<package>/...
                for child in inner[0].iterdir(): child.rename(dest / child.name)
P("=== wheels"); [P(f"  {k}: {v}") for k, v in wheel_status.items()]

# ── 2. CPython 3.12.10 from python.org: embeddable build (binaries) + source (stdlib) ─
# The GitHub toolcache zip just wraps the official python.org installer, so
# python.org's own artifacts are the right reference.
PZ = ROOT / "cpython_win"; PZ.mkdir(exist_ok=True)
emb = ROOT / f"python-{PYV}-embed-amd64.zip"; src = ROOT / f"Python-{PYV}.tgz"
if not emb.exists(): urllib.request.urlretrieve(f"https://www.python.org/ftp/python/{PYV}/python-{PYV}-embed-amd64.zip", emb)
if not src.exists(): urllib.request.urlretrieve(f"https://www.python.org/ftp/python/{PYV}/Python-{PYV}.tgz", src)
EMB = PZ / "embed"; EMB.mkdir(exist_ok=True); zipfile.ZipFile(emb).extractall(EMB)
import tarfile
if not (PZ / f"Python-{PYV}").exists(): tarfile.open(src).extractall(PZ)
LIB = PZ / f"Python-{PYV}" / "Lib"
P("=== cpython: embed zip sha256", sha(emb.read_bytes()), "| source tgz sha256", sha(src.read_bytes()))
P("stdlib Lib at", LIB)

# ── 3. source roots for module resolution ────────────────────────────────────
TAG = ROOT / "tagsrc"; TAG.mkdir(exist_ok=True)
for n in subprocess.run(["git", "-C", W, "ls-tree", "--name-only", "v3.11.4", "src/"], capture_output=True, text=True).stdout.split():
    if n.endswith(".py"):
        (TAG / Path(n).name).write_bytes(subprocess.run(["git", "-C", W, "show", f"v3.11.4:{n}"], capture_output=True).stdout)
PYI = WX / "pyinstaller"
roots = [TAG, LIB] + sorted(WX.iterdir()) + [PYI / "PyInstaller/loader", PYI / "PyInstaller/hooks/rthooks", PYI / "PyInstaller/fake-modules"]
def find_source(mod):
    rel = mod.replace(".", "/")
    for r in roots:
        for cand in (r / (rel + ".py"), r / rel / "__init__.py"):
            if cand.is_file(): return cand
    return None
def same(co, path):
    try:
        return compile(path.read_bytes(), co.co_filename, "exec", dont_inherit=True, optimize=0) == co
    except Exception as e:
        return f"compile error {e}"

# ── 4. every embedded module ─────────────────────────────────────────────────
car = CArchiveReader(str(EXE))
pyz = car.open_embedded_archive([n for n, e in car.toc.items() if e[4] == "z"][0])
items = {n: pyz.extract(n) for n in pyz.toc}
for n, e in car.toc.items():
    if e[4] == "s":
        d = car.extract(n); items[f"<script>{n}"] = marshal.loads(d) if isinstance(d, (bytes, bytearray)) else d
bl = zipfile.ZipFile(INTERNAL / "base_library.zip")
for zi in bl.infolist():
    if zi.filename.endswith(".pyc"):
        mod = zi.filename[:-4].replace("/", ".").removesuffix(".__init__")
        items[f"<base_library>{mod}"] = marshal.loads(bl.read(zi)[16:])
ok, diff, nosrc, notcode = [], [], [], []
for key, co in sorted(items.items()):
    mod = key.split(">", 1)[1] if key.startswith("<") else key
    if not isinstance(co, types.CodeType): notcode.append(key); continue
    src = find_source(mod)
    if src is None: nosrc.append(key); continue
    r = same(co, src)
    (ok if r is True else diff).append((key, str(src.relative_to(ROOT)), r))
P(f"\n=== embedded Python code: {len(items)} objects")
P(f"  MATCH public source : {len(ok)}")
P(f"  DIFFER from source  : {len(diff)}"); [P("    ", k, "<-", s, r) for k, s, r in diff]
P(f"  no source located   : {len(nosrc)}"); [P("    ", k) for k in nosrc]
P(f"  not code objects    : {len(notcode)}"); [P("    ", k) for k in notcode[:20]]

# ── 5. every binary in _internal ─────────────────────────────────────────────
index = {}
for r in [PZ] + sorted(WX.iterdir()):
    for p in r.rglob("*"):
        if p.suffix.lower() in (".pyd", ".dll", ".exe", ".so"):
            index.setdefault(p.name.lower(), set()).add(sha(p.read_bytes()))
bok, bbad, bmiss = [], [], []
for p in sorted(INTERNAL.rglob("*")):
    if p.suffix.lower() in (".pyd", ".dll", ".exe", ".so"):
        h = sha(p.read_bytes()); cands = index.get(p.name.lower())
        rel = str(p.relative_to(INTERNAL))
        if cands is None: bmiss.append(rel)
        elif h in cands: bok.append(rel)
        else: bbad.append(rel)
P(f"\n=== binaries in _internal: {len(bok)+len(bbad)+len(bmiss)}")
P(f"  sha256 MATCH wheel/CPython build : {len(bok)}")
P(f"  sha256 MISMATCH                  : {len(bbad)}"); [P("    ", x) for x in bbad]
P(f"  no reference file found          : {len(bmiss)}"); [P("    ", x) for x in bmiss]

# ── 6. bootloader: .text of Domestique.exe vs PyInstaller's runw.exe ─────────
def text_section(b):
    pe = struct.unpack_from("<I", b, 0x3c)[0]; nsec = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]; sec = pe + 24 + opt_size
    out = {}
    for i in range(nsec):
        name = b[sec + 40*i: sec + 40*i + 8].rstrip(b"\0").decode(errors="replace")
        vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", b, sec + 40*i + 8)
        out[name] = sha(b[rptr: rptr + rsize])
    return out
ours = text_section(EXE.read_bytes())
runw = next(PYI.rglob("Windows-64bit-intel/runw.exe"), None)
if runw:
    ref = text_section(runw.read_bytes())
    P("\n=== bootloader sections vs PyInstaller 6.22.2 runw.exe (resources/.rsrc legitimately differ: icon + version info)")
    for s in ours: P(f"  {s:8s} {'MATCH' if ref.get(s) == ours[s] else 'differs'}")
P("\nDONE")
