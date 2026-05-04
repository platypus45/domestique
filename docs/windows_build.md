# Windows .exe build plan

**Status as of v1.0.0:** macOS DMG ships; Windows badge in the README claims Windows support but no `.exe` is produced. This document is the path to closing that gap.

---

## Goals

A v1.0 Windows release should deliver:

1. A **single zipped or installer-wrapped artifact** the user double-clicks to run.
2. **No install-time friction beyond Windows SmartScreen** (which is unavoidable without an Authenticode certificate). User can click "More info → Run anyway" once.
3. **All v1.0 functionality** — planner, library browser, FIT import, ICU sync, Hooper form, capability projection, finished-programme summary PNG export, the lot.
4. **Single-user, localhost-only** semantics matching the macOS build (no shared install state, no system service).
5. **Built reproducibly in CI**, not hand-cranked from a laptop. Otherwise it's not a release process, it's a one-off.

Non-goals for v1.0:
- ARM64 Windows. x64 only initially.
- Code signing. Plan for v1.1; cost analysis below.
- Microsoft Store distribution. The DMG/EXE direct-download flow is fine for the audience.

---

## Approach

**PyInstaller + Inno Setup + GitHub Actions on `windows-latest`.**

Three layers, each well-trodden:

| Layer | Purpose | Tool |
|---|---|---|
| Bundling | freeze Python interpreter + all deps + ZWO library + routes JSON into a portable directory | **PyInstaller --onedir** (NOT --onefile — see below) |
| Installer | wrap the directory in a `setup.exe` that creates Start Menu shortcuts, an uninstaller, and writes user data to `%USERPROFILE%\.domestique\` | **Inno Setup 6** (free, scriptable, ships on every Windows dev box) |
| CI | run both steps on every tag push, attach the artefact to the GitHub release | **GitHub Actions** runner `windows-latest` (Server 2022, free for public + private repos within quota) |

### Why `--onedir` not `--onefile`

`--onefile` creates a single `.exe` that self-extracts to a temp dir on every launch. That adds 3–8 seconds of startup time AND triggers a fresh SmartScreen prompt every install. `--onedir` produces a folder with `Domestique.exe` + DLLs alongside; Inno Setup ships the whole folder as one installer. Startup is instant after install, and Windows treats it as a normal application.

### Why an installer, not just a zip

The user's existing `build_win.bat` (referenced in README at line 165) writes `dist\Domestique\Domestique.exe`. That's the PyInstaller --onedir output. Could be zipped and shipped as-is — but:

- No Start Menu shortcut.
- No uninstaller (user has to delete the folder by hand).
- No file-association registration (FIT files won't open in Domestique by double-click).
- Microsoft Defender's reputation system penalises unsigned standalone .exe more harshly than packaged installers.

Inno Setup adds ~5 minutes to the build pipeline and removes all four problems.

---

## Implementation steps (in order)

### 1. Confirm `build_win.bat` actually works on a clean `windows-latest` runner

The script exists in the repo but may have rotted — it predates several of the v4.6.x deps additions. Before writing CI, check it manually on a Windows VM or via a one-shot Actions workflow:

```yaml
# .github/workflows/win-smoke.yml (temporary)
on: workflow_dispatch
jobs:
  smoke:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: pip install pyinstaller
      - run: .\build_win.bat
      - uses: actions/upload-artifact@v4
        with: { name: dist, path: dist\Domestique }
```

Run via "Workflow dispatch" in the Actions tab. Download the artifact, run `Domestique.exe` on a real Windows 10/11 machine, click through the home page + plan + library + FIT import. Catch missing-dep errors before automating.

Likely fixes:
- PyInstaller hidden-imports for late-binding modules (`uvicorn.workers`, `pywebview.platforms.winforms`, `lxml._elementpath`).
- Add `--collect-data` for the `workouts/`, `routes/`, `templates/`, `nutrition/` folders so they ship inside the bundle.
- Pin `pywebview==5.x` since 4.x has a Windows-specific bug with WebView2 init order.

### 2. Write the PyInstaller spec file

Replace `build_win.bat`'s inline arguments with a `domestique.win.spec` file checked into the repo. Pinning the spec means CI is deterministic and the user can build locally with `pyinstaller domestique.win.spec`.

Key entries:

```python
# domestique.win.spec (sketch)
a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('workouts/*.zwo', 'workouts'),
        ('workouts/.content_classification.json', 'workouts'),
        ('templates/*.html', 'templates'),
        ('static/**', 'static'),
        ('routes/*.json', 'routes'),
        ('nutrition/*.json', 'nutrition'),
        ('assets/icon.ico', 'assets'),
        ('VERSION', '.'),
    ],
    hiddenimports=[
        'uvicorn.workers',
        'pywebview.platforms.winforms',
        'lxml._elementpath',
        'fitparse',
        'PIL._tkinter_finder',
    ],
    ...
)

exe = EXE(pyz, a.scripts, [],
    name='Domestique',
    icon='assets/icon.ico',
    console=False,            # no terminal window
    disable_windowed_traceback=False,
    target_arch='x86_64',
)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
    name='Domestique')
```

Result: `dist\Domestique\Domestique.exe` + supporting files.

### 3. Inno Setup script

Add `installer\domestique.iss`:

```iss
#define MyAppName "Domestique"
#define MyAppPublisher "Domestique Project"
#define MyAppURL "https://github.com/platypus45/domestique"
#define MyAppExeName "Domestique.exe"
#define MyAppVersion "1.0.0"  ; replaced by CI from VERSION file

[Setup]
AppId={{D04E5C9A-DA31-4B85-9C5F-DOMESTIQUE-1000}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
PrivilegesRequired=lowest         ; per-user install, no admin prompt
ArchitecturesAllowed=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=Domestique-{#MyAppVersion}-Setup
SetupIconFile=..\assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Domestique\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
```

Output: `Domestique-1.0.0-Setup.exe` (~70 MB after lzma2/ultra64 compression).

### 4. GitHub Actions workflow

Add `.github/workflows/release-win.yml`:

```yaml
name: Build Windows installer
on:
  push:
    tags: ['v*.*.*']
  workflow_dispatch:

jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: pip

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install pyinstaller

      - name: Read VERSION
        id: ver
        shell: bash
        run: echo "version=$(cat VERSION)" >> $GITHUB_OUTPUT

      - name: PyInstaller
        run: pyinstaller domestique.win.spec --noconfirm

      - name: Smoke test exe (10s headless)
        run: |
          Start-Process -FilePath dist\Domestique\Domestique.exe -ArgumentList "--smoke" -PassThru
          Start-Sleep -Seconds 10
          Stop-Process -Name Domestique -ErrorAction SilentlyContinue

      - name: Install Inno Setup
        run: choco install innosetup --no-progress

      - name: Build installer
        run: |
          $env:Path += ';C:\Program Files (x86)\Inno Setup 6'
          ISCC.exe /DMyAppVersion=${{ steps.ver.outputs.version }} installer\domestique.iss

      - name: SHA-256 the installer
        shell: bash
        run: |
          cd dist/installer
          sha256sum Domestique-${{ steps.ver.outputs.version }}-Setup.exe \
            > Domestique-${{ steps.ver.outputs.version }}-Setup.exe.sha256

      - name: Upload to release
        if: startsWith(github.ref, 'refs/tags/')
        uses: softprops/action-gh-release@v1
        with:
          files: |
            dist/installer/Domestique-${{ steps.ver.outputs.version }}-Setup.exe
            dist/installer/Domestique-${{ steps.ver.outputs.version }}-Setup.exe.sha256
```

Trigger: tag push (matches macOS release flow). Manual dispatch for ad-hoc builds. The smoke step needs `launcher.py` to support a `--smoke` flag that boots, serves one health-check, and exits — small backend change.

### 5. Documentation

Update README "Quick Start → Download" table to point to the new artifact name (`Domestique-1.0.0-Setup.exe`). Update "Installing on Windows (SmartScreen)" to mention the Setup.exe and the per-user install location (`%LOCALAPPDATA%\Programs\Domestique\`).

---

## Code signing — the SmartScreen story

Without a code signing certificate, every fresh Windows install of `Domestique-1.0.0-Setup.exe` shows:

> Windows protected your PC. Microsoft Defender SmartScreen prevented an unrecognised app from starting.

User clicks "More info" → "Run anyway". This is annoying but not blocking. Most open-source Windows apps below the popularity threshold ship like this (e.g. early Obsidian, early VS Code Insiders, every PyInstaller-built app you've ever downloaded).

To suppress the warning we'd need:

| Cert type | Vendor | Cost | Effect |
|---|---|---|---|
| **OV (organisation-validated)** | DigiCert / Sectigo / SSL.com | $200–$300/year | Removes warning AFTER reputation accrues (need ~3000 installs over ~30 days) |
| **EV (extended-validation)** | DigiCert / Sectigo | $300–$500/year + USB token | Removes warning **immediately** on every install. Token shipped via courier, reissued annually. |

Only buy EV. OV is a ramp-up trap — by the time you accrue reputation, the cert is mid-lifecycle.

Steps once a cert is acquired:
1. Install signing tool: `signtool.exe` (Windows SDK) or `osslsigncode` (cross-platform).
2. Add a CI step that signs `Domestique.exe` AND the `Setup.exe`:
   ```
   signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 \
     /a Domestique.exe Setup.exe
   ```
3. Cert + private key live in GitHub Actions secrets (encrypted, only used at sign time).

**Recommendation for v1.0:** ship unsigned, document the SmartScreen click-through. Buy EV when the user count justifies the $400/year — i.e. the moment the friction starts costing real adoption. The OSS hobbyist audience tolerates SmartScreen one-click; the cert is a v1.x improvement, not a v1.0 blocker.

---

## Test matrix

Run the smoke checklist on each target before tagging:

|  | Win 10 22H2 x64 | Win 11 23H2 x64 |
|---|---|---|
| Setup.exe runs without admin | ✅ required | ✅ required |
| Domestique.exe launches in <3s | ✅ | ✅ |
| Home page renders within 5s of launch | ✅ | ✅ |
| Settings → ICU credential entry persists across restart | ✅ | ✅ |
| Plan generation completes (~10s) | ✅ | ✅ |
| Library browser loads all 3,054 ZWOs | ✅ | ✅ |
| FIT import: drag-drop ride.fit → activity record stored | ✅ | ✅ |
| Programme summary modal renders + Export PNG works | ✅ | ✅ |
| Uninstaller removes everything except `~\.domestique\` | ✅ | ✅ |
| Reinstall over existing install: no "in use" errors | ✅ | ✅ |

---

## Effort + timeline

| Step | Effort | Skill needed |
|---|---|---|
| Step 1 confirm build_win.bat | 1–2 days iterating PyInstaller deps on a Windows VM / Actions runner | Python + Windows packaging |
| Step 2 spec file | 0.5 day once step 1 lands | Python |
| Step 3 Inno Setup script | 0.5 day | Inno Setup syntax (similar to NSIS — straightforward) |
| Step 4 GitHub Actions workflow | 0.5 day | GitHub Actions YAML |
| Step 5 docs + README update | 0.25 day | — |
| Smoke test on real Windows | 0.5 day | — |
| **Total** | **~4 days of focused work** | |

**Critical path:** Step 1 dominates. PyInstaller failures on Windows are silent — wrong hidden-imports causes the .exe to launch and immediately exit with no log. Allow a couple of debug iterations.

---

## Reference

- PyInstaller manual: https://pyinstaller.org/en/stable/
- Inno Setup directives: https://jrsoftware.org/ishelp/
- GitHub Actions windows-latest specs: https://github.com/actions/runner-images
- Microsoft signing best practices: https://learn.microsoft.com/en-us/windows/win32/seccrypto/cryptography-tools
