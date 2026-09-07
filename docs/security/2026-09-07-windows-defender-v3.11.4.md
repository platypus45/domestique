# Windows Defender flags Domestique 3.11.4 — provenance audit

**Date:** 2026-09-07  ·  **Trigger:** [issue #12](https://github.com/platypus45/domestique/issues/12) — Windows Defender reports `Trojan:Win32/Sabsik.TE.A!ml` on the 3.11.4 Windows build; VirusTotal shows 7 of 68 engines flagging.  ·  **Scope:** the file the report names, and the two previous Windows builds.

## Verdict

**False positive.** Every byte of executable content in the shipped bundle traces to a public source or a verified vendor signature:

| What | How verified | Result |
|---|---|---|
| The reported file | SHA-256 of the EXE inside our release zip | identical to the hash in the report |
| Who built it | GitHub release-asset metadata | uploaded by `github-actions[bot]` from the tag build, not from any personal machine |
| 1,987 embedded Python code objects (our app, stdlib, all dependencies, PyInstaller loader) | recompiled from public source and compared as code objects | **1,978 match, 0 differ, 0 unlocated**; 9 are empty namespace packages (no code) |
| 42 dependency wheels the runner installed | SHA-256 against PyPI's published digests | **42 of 42 match** |
| 294 DLL/pyd binaries | SHA-256 against the PyPI wheel or python.org's own build | **246 match, 0 mismatch**; 48 have no hash reference → next row |
| those 48 (Universal CRT forwarders, Tcl/Tk, `_tkinter`) | Authenticode signature: digest + chain to Microsoft's roots + timestamp | **48 of 48 valid**: 44 signed by Microsoft Corporation, 4 by the Python Software Foundation |
| all 75 top-level binaries | Authenticode | 74 valid signatures (44 Microsoft, 2 Microsoft Windows Software Compatibility Publisher, 28 PSF); the 1 unsigned file (`_cffi_backend`) is a PyPI wheel file that matched PyPI by hash |
| the EXE's own code (PyInstaller bootloader) | section-by-section against PyInstaller 6.22.2's stock `runw.exe` from PyPI | `.text .data .pdata .fptable .reloc` **identical**; `.rdata` differs in 3 bytes = a build timestamp; `.rsrc` = our icon + version info (data) |
| 6,003 data files (workouts, courses, templates, static, assets…) | git blob hash against the tagged tree | 6,001 identical (5,451 after CRLF normalisation — Windows checkout); 2 are indexes regenerated at build time, differing only in a modification-time field |
| 926 Tcl/Tk data files | byte comparison with the 3.11.2 and 3.11.3 bundles | identical to both earlier builds |
| Network/behaviour strings | every URL/domain string in the bundle; injection/keylogger/miner API names | only expected domains (Microsoft CRL/OCSP inside signed DLLs, numpy/scipy docs, github.com, intervals.icu, garmin support, python.org); **0** injection/miner strings |
| Listening socket | source | binds `127.0.0.1` only (`src/launcher.py:441`, `src/app.py:22196`) |

Nothing in the bundle came from anywhere other than: this repository at tag `v3.11.4`, PyPI (exact versions, hash-verified), python.org (signed by the PSF), Microsoft (signed), and PyInstaller 6.22.2 (from PyPI). There is no unexplained code.

## The four questions

**1. Did we import contaminated packages?** No. The only package added in 3.11.4 is `truststore 0.10.4` (pure Python, by the Python Software Foundation's security developer-in-residence). Its wheel hash equals PyPI's published digest (uploaded 2025-08-12) and each of its six modules inside the EXE recompiles identically from that wheel. The only other third-party change between 3.11.3 and 3.11.4 is `anyio` moving to 4.15.0 on the build runner; its wheel hash equals PyPI's (uploaded 2026-09-02 by its maintainer) and all 37 modules match. **All compiled binaries (every DLL and pyd) are byte-identical across 3.11.2, 3.11.3 and 3.11.4.**

**2. Why did this appear now?** The build has always been an unsigned PyInstaller executable — the class of file that machine-learning detectors flag most often; the `!ml` suffix in `Sabsik.TE.A!ml` / `Wacatac.C!ml` means a statistical model, not a signature of known malware. Three things changed the file's "shape" between 3.11.2 and 3.11.4, all deliberate and all in the changelog: 3.11.3 started reading the Windows certificate store (to fix sign-in behind antivirus HTTPS scanning); 3.11.4 added `truststore`, which calls Windows' certificate-chain APIs (`CertGetCertificateChain`, `CertVerifyCertificateChainPolicy`, an *in-memory* `CertOpenStore` — it never writes to the system store) through `ctypes`; and code grew accordingly. Certificate-store API use inside an unsigned, self-extracting executable is exactly the pattern these models weight. Microsoft's cloud model also re-scores files over time, so identical files can flip. The reporter states 3.11.2 was the last build Defender accepted; neither the 3.11.2 nor the 3.11.3 EXE has ever been submitted to VirusTotal, so no independent history exists for them.

**3. Is it a known false-positive class?** Yes. `Wacatac.*!ml` / `Sabsik.*!ml` on unsigned PyInstaller output is one of the most reported false positives in the Python packaging ecosystem: [pyinstaller/pyinstaller#5854](https://github.com/pyinstaller/pyinstaller/issues/5854), [PythonGUIs FAQ](https://www.pythonguis.com/faq/problems-with-antivirus-software-and-pyinstaller/), [discuss.python.org thread](https://discuss.python.org/t/pyinstaller-false-positive/43171), [a maintainer's write-up](https://medium.com/@markhank/how-to-stop-your-python-programs-being-seen-as-malware-bfd7eb407a7), and a [repository documenting the reporting process](https://github.com/hankhank10/false-positive-malware-reporting). The VirusTotal split matches the class: the seven flags are all heuristic/ML engines (Microsoft `!ml`, SentinelOne "Static AI – Suspicious PE", SecureAge "Malicious", Arctic Wolf "Unsafe", Huorong "Python.ShellLoader", Bkav, Zillya); Bitdefender, Avast, AVG, Avira, AhnLab, Alibaba, Arcabit, ALYac, Antiy and the other signature engines report clean. Huorong's own label names the real content: a Python loader.

**4. Revert to 3.11.3?** Not recommended. (a) The reporter says 3.11.3 is flagged as well — the flag predates `truststore`. (b) Every byte that changed between 3.11.3 and 3.11.4 is verified above; a revert removes a real fix (Windows riders behind antivirus HTTPS scanning could not sign in or sync) without removing the cause of the flag. (c) The fixes that actually address the flag are below.

## What to do

1. **Submit a false-positive report to Microsoft** — [Microsoft Security Intelligence file submission](https://www.microsoft.com/wdsi/filesubmission), as "Software developer", with `Domestique-Windows.zip` from the release and the hash below. Wacatac/Sabsik `!ml` disputes are typically cleared within days. (Owner action: needs a Microsoft account.)
2. **Code-sign the Windows build.** The durable fix; an unsigned executable is the dominant feature these models key on, and SmartScreen reputation is built on the signing identity. Options: Azure Trusted Signing (what the PSF itself uses for python.org binaries), SignPath Foundation (free for open source), or an OV certificate. The CI workflow already has the `signtool` step drafted.
3. **Publish the hash and this audit** on the release page so users can verify what they downloaded: `Domestique.exe` SHA-256 `48068373987a6f844aa5169de948f72820332b5c58eeeb1c21bb79e20e4c6b93`; `Domestique-Windows.zip` SHA-256 `3f9fb405ba21b8d60b8ed061ac3f0533a40d2d648c09458fe63f45c3dd9cfbfc`.
4. Optional hardening that reduces heuristic hits: build the PyInstaller bootloader from source in CI (removes the shared stock-bootloader fingerprint); keep UPX off (it is off — no UPX markers in the EXE).

## Evidence, reproducibly

Everything below was run on 2026-09-07 on macOS against the public release assets. Paths are relative to a scratch directory; `unz/` is the unzipped `Domestique-Windows.zip`.

### E1 — the reported hash is our file, built by CI

```bash
gh release download v3.11.4 --repo platypus45/domestique --pattern 'Domestique-Windows.zip'
shasum -a 256 Domestique-Windows.zip      # 3f9fb405ba21b8d60b8ed061ac3f0533a40d2d648c09458fe63f45c3dd9cfbfc  (83,296,634 B)
unzip -q Domestique-Windows.zip -d unz
shasum -a 256 unz/Domestique.exe          # 48068373987a6f844aa5169de948f72820332b5c58eeeb1c21bb79e20e4c6b93  (16,146,283 B)  == the hash in the report
gh api repos/platypus45/domestique/releases/tags/v3.11.4 --jq '.assets[] | "\(.name) \(.uploader.login) \(.created_at)"'
#   Domestique-Windows.zip  github-actions[bot]  2026-09-05T08:06:21Z     (run 33954169244, tag v3.11.4 -> commit ba440379)
#   Domestique-v3.11.4-x86_64.AppImage  github-actions[bot]
#   Domestique-v3.11.4.dmg  platypus45   (built locally, notarized by Apple — Apple's notarization includes a malware scan)
```

Earlier builds, for comparison (never uploaded to VirusTotal by anyone):

| build | `Domestique.exe` SHA-256 | size |
|---|---|---|
| 3.11.2 | `8767843c1c25b6edf88d754e8db1644ca5b57f1f059a54eb5b1f4db9d8b5a999` | 16,105,273 |
| 3.11.3 | `0b30a538f580d187f776007bf4adace33195866752120078a2a41cbe8b8788c4` | 16,110,455 |
| 3.11.4 | `48068373987a6f844aa5169de948f72820332b5c58eeeb1c21bb79e20e4c6b93` | 16,146,283 |

### E2 — what changed between builds (whole bundle, 7,291 files)

Per-file SHA-256 of the unzipped 3.11.3 and 3.11.4 bundles: **5 files differ**, none of them a compiled binary:
`Domestique.exe` (carries the Python code, see E3), `_internal/VERSION`, `_internal/base_library.zip` (stdlib, see E3), `_internal/numpy-2.5.2.dist-info/RECORD`, `_internal/workouts/.library_index.json` (build-time index).
3.11.2 → 3.11.3: 11 files differ — the same three build artefacts plus our own `templates/dashboard.html`, three workout files and two workout indexes. **Every `.dll` and `.pyd` is byte-identical across all three releases.**

### E3 — every embedded module against its public source

`packaging/audit_windows_bundle.py` (in this repository) opens the PyInstaller archive inside the EXE, collects every code object (1,825 PYZ modules + 7 runtime scripts + 155 stdlib `.pyc` from `base_library.zip` = 1,987), locates each module's source in: this repository at `v3.11.4` (`git show`), CPython 3.12.10's `Lib/` (python.org source tarball, SHA-256 `15d9c623…6dac`), the exact wheels listed in the CI log's `Successfully installed` line (all 42 hash-verified against PyPI's JSON API), and PyInstaller 6.22.2's loader/hook directories — recompiles it (`compile(src, name, "exec", optimize=0)`) and compares code objects (`==`, which compares bytecode, constants, names, line tables).

```
=== embedded Python code: 1987 objects
  MATCH public source : 1978
  DIFFER from source  : 0
  no source located   : 0
  not code objects    : 9      (namespace packages: fit_tool.profile, scipy._external, setuptools._vendor, … — no code by definition)
```

Module-level delta 3.11.3 → 3.11.4 inside the EXE: added `tls_trust`, `truststore` (+5 submodules), 3 `anyio` internals; removed `anyio.from_thread`; changed 27 = 23 `anyio` modules (version 4.15.0) + our `app`, `icu_calendar_push`, `training`, `training_planner`. 3.11.2 → 3.11.3: changed 4 (`app`, `fit_activity`, `fitness_estimation`, `ride_storage`), added/removed none. The entry script `launcher` (stored outside the PYZ) also matches the tagged source.

### E4 — every binary against PyPI / python.org / vendor signatures

```
=== binaries in _internal: 294
  sha256 MATCH wheel/CPython build : 246
  sha256 MISMATCH                  : 0
  no reference file found          : 48   (api-ms-win-*.dll ×43, ucrtbase.dll, _tkinter.pyd, tcl86t.dll, tk86t.dll, zlib1.dll)
```

References: the 42 wheels (`pip download --only-binary=:all: --platform win_amd64 --python-version 3.12 …`, each SHA-256 checked against `https://pypi.org/pypi/<name>/<ver>/json`) and python.org's `python-3.12.10-embed-amd64.zip` (SHA-256 `4acbed6d…a3c3`). The GitHub Actions toolcache for Windows is the official python.org installer (`python-3.12.10-amd64.exe` inside `actions/python-versions` release `3.12.10-14343898437`), so python.org is the correct reference.

The 48 files without a hash reference ship inside the python.org *installer* (Universal CRT, Tcl/Tk) rather than the embeddable zip, so they were verified by signature instead. Microsoft's roots were fetched from Microsoft's PKI repository and appended to the Mozilla bundle:

```bash
curl -o ms_idv_root_2020.crt "https://www.microsoft.com/pkiops/certs/Microsoft%20Identity%20Verification%20Root%20Certificate%20Authority%202020.crt"   # SHA-256 5367F20C…2E1270
curl -o ms_root_2010.crt "https://www.microsoft.com/pki/certs/MicRooCerAut_2010-06-23.crt"                                                            # SHA-256 DF545BF9…3C163E
cat "$(python3 -c 'import certifi;print(certifi.where())')" ms_idv_root_2020.pem ms_root_2010.pem > combined_ca.pem
for f in unz/_internal/*.dll unz/_internal/*.pyd; do osslsigncode verify -in "$f" -CAfile combined_ca.pem -TSA-CAfile combined_ca.pem; done
```

Result over all 75 top-level binaries: 74 × `Signature verification: ok` (digest recomputed and equal, chain to a Microsoft root, RFC 3161 timestamp verified) — 44 Microsoft Corporation, 2 Microsoft Windows Software Compatibility Publisher (`VCRUNTIME140*.dll`), 28 Python Software Foundation (`python312.dll`, every stdlib `.pyd`, `libssl-3.dll`, `libcrypto-3.dll`, `sqlite3.dll`, `libffi-8.dll`, `tcl86t.dll`, `tk86t.dll`, `zlib1.dll`, `_tkinter.pyd`); 1 unsigned (`_cffi_backend.cp312-win_amd64.pyd`, a PyPI wheel file, matched PyPI by SHA-256 in the previous step). Sample: `python312.dll` — current digest `B9C31D24…BE719` = calculated digest, timestamp 2025-04-08 by Microsoft Public RSA Time Stamping Authority.

### E5 — the EXE itself is PyInstaller's stock bootloader plus our archive

PE sections of `unz/Domestique.exe` vs `PyInstaller/bootloader/Windows-64bit-intel/runw.exe` from the `pyinstaller-6.22.2-py3-none-win_amd64.whl` (hash-verified against PyPI):

```
.text    MATCH      .rdata  differs (3 bytes)      .data   MATCH      .pdata  MATCH
.fptable MATCH      .rsrc   differs                .reloc  MATCH
```

The 3 `.rdata` bytes are the debug directory's `TimeDateStamp` (`0x6a9bcd2b` = 2026-09-05, build day; the wheel's is `0x6a834124` = 2026-08-17) — PyInstaller re-stamps the header when it adds resources. `.rsrc` (40,976 bytes) is the added icon and version-info resource — data, not code. Everything after the last section (offset 324,096 to the end) is the PyInstaller archive audited in E3. No UPX markers in any build.

### E6 — data files against the tagged tree

Git blob hashes of every non-binary file under `_internal/` that the repository owns, against `git ls-tree -r v3.11.4`: 550 identical; 5,451 identical after `\r\n` → `\n` (the Windows runner checks text out with CRLF); 2 differ — `workouts/.library_index.json` (only its `max_mtime` field, a build timestamp) and `workouts/.workout_facts.json` (identical to 3.11.3's copy). `assets/` (12 files): identical. Files not in the repository: `VERSION`, `.oauth.env` (two expected keys — the OAuth client id and secret, by design), and 926 Tcl/Tk data files under `_tcl_data/`, `_tk_data/`, `tcl8/` — byte-identical to the 3.11.2 and 3.11.3 bundles (Tk is a fallback file-dialog path; `tk86t.dll`/`tcl86t.dll` are PSF-signed).

### E7 — indicator sweep

Every `http(s)://` string in every file of the bundle, by domain: `microsoft.com` (certificate CRL/OCSP URLs inside the Microsoft-signed DLLs), `numpy.org`, `dlmf.nist.gov`, `wikipedia.org`, `boost.org`, `github.com` (update check + scipy references), `netlib.org`, `arxiv.org`, `support.garmin.com`, `intervals.icu` (4), `python.org`, `digicert.com` (signing chains), assorted maths references from scipy. Zero occurrences of `WriteProcessMemory`, `CreateRemoteThread`, `VirtualAllocEx`, `SetWindowsHookEx`, `GetAsyncKeyState`, `stratum+tcp`, `xmrig`, `.onion` in `Domestique.exe` + `python312.dll`. No `certutil`, `hosts`, `powershell` or `cmd.exe /c` strings — the CI test helper that uses those (`packaging/tls_intercept_probe_win.py`) is not in the bundle (`unzip -l | grep -i probe` → nothing).

### What this audit does not cover

- It does not execute the binary in a sandbox; the VirusTotal "Behavior" tab was not consulted. The CI smoke test runs the EXE on every release and asserts what it serves; the app binds `127.0.0.1` only.
- The 926 Tcl/Tk data files were matched to the previous two builds, not to an external reference (they live inside the python.org installer's MSI, which was not unpacked here).
- The `.rsrc` resource block (icon, version strings) was inspected only by size and section type.
- A compromise of the GitHub Actions runner itself would have had to emit output byte-identical to the public sources checked above to go unnoticed; that is the guarantee this audit provides, not a statement about the runner.

## Reproduce it yourself

```bash
# 1. the reported file
gh release download v3.11.4 --repo platypus45/domestique --pattern 'Domestique-Windows.zip' && unzip -q Domestique-Windows.zip -d unz && shasum -a 256 unz/Domestique.exe
# 2. exact dependency versions the runner installed (Windows job log, "Successfully installed" line)
gh run view 33954169244 --repo platypus45/domestique --log | grep -a "Successfully installed" | tr ' ' '\n' | grep -v "Successfully\|installed" | sort -u > installed.txt
# 3. full audit (needs the build venv: PyInstaller's archive reader)
.venv-build/bin/python packaging/audit_windows_bundle.py <repo-root> installed.txt      # writes verify_report.txt
# 4. signatures of the remaining files
brew install osslsigncode  # or any Authenticode verifier
```
