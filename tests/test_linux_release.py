"""v3.8.0 LINUX-RELEASE — cross-domain guards for the AppImage release.

The Linux release edits four files that the shipping macOS DMG and Windows
EXE also depend on, and it is authored on a Mac that cannot run any of the
Linux code. So the invariants that matter most are exactly the ones no
developer machine will ever exercise, and they are pinned here instead:

  1. Adding a Linux asset to a release must not change what a macOS or a
     Windows user is offered by the update banner (`app._select_platform_asset`).
  2. Qt must resolve to Linux ONLY. A missing marker in requirements.txt
     silently pulls ~300 MB of PySide6 into the DMG builder's venv; and PyQt6
     may not appear at all — it is GPL/commercial-licensed and this project
     ships under Apache-2.0.
  3. The frozen Linux bundle must carry the Qt web-view modules PyInstaller's
     static scan cannot see, and must not be handed a Windows .ico.
  4. Backend failure on Linux must be fatal and visible. The pre-v3.8.0 path
     ended in a live process holding the port with no window, no tray, no
     error and no exit (MASTER_DECISIONS_LINUX §7) — the single worst
     failure mode of this release.

Assertions whose owning agent has not landed its change yet SKIP rather than
fail, so this file is committable before the other domains are; an assertion
about work that HAS landed always runs.
"""
from __future__ import annotations

import ast
import os
import types
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

# §11: frozen literal. The v1.8.8 DMG incident came from a bare, unversioned
# asset name, so the version token is deliberate and pinned here as a string.
APPIMAGE_NAME = f"Domestique-v{VERSION}-x86_64.AppImage"

_BASE = "https://github.com/platypus45/domestique/releases/download/v" + VERSION


def _asset(name: str) -> dict:
    return {"name": name, "browser_download_url": f"{_BASE}/{name}"}


# A realistic v3.8.0 release: all three platforms' artifacts side by side.
_RELEASE_ASSETS = [
    _asset("Domestique.dmg"),
    _asset(APPIMAGE_NAME),
    _asset("Domestique-Windows.zip"),
    _asset("Domestique.exe"),
]


# ─── 1. update picker ────────────────────────────────────────────────────────

def _picker():
    import app
    return app._select_platform_asset


def test_macos_and_windows_picks_are_unchanged_by_the_linux_asset():
    """The regression that would be invisible on a Mac: a stray `.AppImage`
    reordering or shadowing the pick for the two platforms that ship today."""
    pick = _picker()

    assert pick(_RELEASE_ASSETS, "darwin") == (
        f"{_BASE}/Domestique.dmg", "Domestique.dmg")
    assert pick(_RELEASE_ASSETS, "win32") == (
        f"{_BASE}/Domestique.exe", "Domestique.exe")

    # Decorated .dmg only → still picked; canonical name preferred when both exist.
    decorated = [_asset(f"Domestique-{VERSION}.dmg"), _asset(APPIMAGE_NAME)]
    assert pick(decorated, "darwin") == (
        f"{_BASE}/Domestique-{VERSION}.dmg", f"Domestique-{VERSION}.dmg")
    assert pick([_asset("Domestique-1.0.3.dmg"), _asset("Domestique.dmg")],
                "darwin") == (f"{_BASE}/Domestique.dmg", "Domestique.dmg")

    # Windows falls back to the .zip only when no .exe is present.
    assert pick([_asset("Domestique-Windows.zip"), _asset(APPIMAGE_NAME)],
                "win32") == (f"{_BASE}/Domestique-Windows.zip",
                             "Domestique-Windows.zip")

    # Nothing for the platform, and defensive inputs, still yield (None, None).
    assert pick([_asset(APPIMAGE_NAME)], "darwin") == (None, None)
    assert pick([_asset(APPIMAGE_NAME)], "win32") == (None, None)
    assert pick([], "darwin") == (None, None)
    assert pick(None, "win32") == (None, None)
    # An unsupported platform must not inherit the Linux arm.
    assert pick(_RELEASE_ASSETS, "freebsd13") == (None, None)


def test_linux_picks_the_versioned_appimage():
    src = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
    if "AppImage" not in src:
        pytest.skip("update picker has no Linux arm yet (CODE agent)")
    pick = _picker()

    assert pick(_RELEASE_ASSETS, "linux") == (f"{_BASE}/{APPIMAGE_NAME}",
                                              APPIMAGE_NAME)
    # No AppImage in the release → offer nothing, so the banner falls back to
    # the release URL rather than handing a Linux user a .dmg.
    assert pick([_asset("Domestique.dmg"), _asset("Domestique.exe")],
                "linux") == (None, None)


# ─── 2. requirements.txt ─────────────────────────────────────────────────────

def _requirements() -> dict[str, Requirement]:
    reqs = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        reqs[req.name.lower()] = req
    return reqs


def test_qt_dependencies_resolve_on_linux_only():
    """No marker → `pip install -r requirements.txt` pulls PySide6 into the
    macOS/Windows build venvs, and the DMG/EXE stop resolving byte-identically
    to today (MASTER_DECISIONS_LINUX §2)."""
    reqs = _requirements()
    missing = [n for n in ("pyside6", "qtpy") if n not in reqs]
    if missing:
        pytest.skip(f"Qt dependencies not declared yet (CODE agent): {missing}")

    for name in ("pyside6", "qtpy"):
        marker = reqs[name].marker
        assert marker is not None, f"{name} carries no environment marker"
        assert marker.evaluate({"sys_platform": "linux"}) is True, name
        assert marker.evaluate({"sys_platform": "darwin"}) is False, name
        assert marker.evaluate({"sys_platform": "win32"}) is False, name


def test_pyqt_is_never_a_dependency():
    """Licensing, not preference: PyQt is GPL-or-commercial and Domestique
    ships under Apache-2.0. PySide6 (LGPL) is the only admissible Qt binding.
    Checked in both places a Qt binding could enter the build."""
    named = [n for n in _requirements() if n.startswith("pyqt")]
    assert named == [], f"PyQt must not be a dependency: {named}"

    spec_src = (ROOT / "packaging" / "domestique.spec").read_text(encoding="utf-8")
    if "_LINUX" in spec_src:
        for plat in ("darwin", "win32", "linux"):
            offenders = [h for h in _spec_hiddenimports(plat)
                         if h.lower().startswith("pyqt")]
            assert offenders == [], f"PyQt hidden import on {plat}: {offenders}"


# ─── 3. domestique.spec ──────────────────────────────────────────────────────

def _spec_tree() -> tuple[str, ast.Module]:
    src = (ROOT / "packaging" / "domestique.spec").read_text(encoding="utf-8")
    return src, ast.parse(src)


def _spec_eval(expr_src: str, plat: str, ns_extra: dict | None = None):
    """Evaluate a literal-only spec expression as the given build platform.

    The spec is not importable (PyInstaller injects Analysis/EXE/COLLECT), so
    the platform-conditional expressions are pulled out and evaluated against
    a synthetic `sys`. That is what makes a macOS test able to assert what a
    Linux PyInstaller run will actually see.
    """
    ns = {"sys": types.SimpleNamespace(platform=plat), "os": os}
    ns.update(ns_extra or {})
    # Parenthesised: a multi-line conditional loses its enclosing parens when
    # it is lifted out of the call site by `ast.get_source_segment`.
    return eval(f"({expr_src})", {"__builtins__": {}}, ns)  # noqa: S307 — repo-owned literals


def _spec_ns(plat: str) -> dict:
    src, tree = _spec_tree()
    ns: dict = {}
    # v3.11.2: the hiddenimports list splices in
    # `*collect_submodules("fit_tool.profile.messages")` (the 90 FIT message
    # modules the static scan never followed). Evaluate it with the real hook
    # when PyInstaller is importable so the list is faithful; otherwise an
    # empty splice keeps the Qt/PyQt assertions evaluable.
    try:
        from PyInstaller.utils.hooks import collect_submodules
    except Exception:  # pragma: no cover — PyInstaller absent in this env
        def collect_submodules(*_a, **_k):
            return []
    ns["collect_submodules"] = collect_submodules
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_LINUX"
                        for t in node.targets)):
            ns["_LINUX"] = _spec_eval(ast.get_source_segment(src, node.value), plat)
    return ns


def _spec_call_kwarg(func_name: str, kwarg: str) -> str:
    src, tree = _spec_tree()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == func_name):
            for kw in node.keywords:
                if kw.arg == kwarg:
                    return ast.get_source_segment(src, kw.value)
    raise AssertionError(f"{func_name}(… {kwarg}=…) not found in domestique.spec")


def _spec_hiddenimports(plat: str) -> list[str]:
    return list(_spec_eval(_spec_call_kwarg("Analysis", "hiddenimports"),
                           plat, _spec_ns(plat)))


def test_linux_hiddenimports_carry_the_qt_webview_modules():
    """pywebview resolves its GUI backend inside `webview.start()`, so nothing
    in the static import scan reaches the Qt platform module. QtWebChannel is
    load-bearing: without it pywebview's qt.py takes a PyQt5/QtWebKit fallback
    branch that does not exist in this bundle."""
    if "_LINUX" not in (ROOT / "packaging" / "domestique.spec").read_text(encoding="utf-8"):
        pytest.skip("spec has no Linux arm yet (PACKAGING agent)")

    linux = _spec_hiddenimports("linux")
    for module in ("QtWebChannel", "QtWebEngineCore"):
        assert any(module in h for h in linux), (
            f"Linux hiddenimports missing {module}: {linux}")

    # …and Qt must not leak into the two builds that ship today: PySide6 is
    # not installed on those builders, so a leaked entry is a hard build fail.
    for plat in ("darwin", "win32"):
        leaked = [h for h in _spec_hiddenimports(plat)
                  if "PySide6" in h or h == "qtpy" or "Qt" in h]
        assert leaked == [], f"Qt hidden imports leaked into {plat}: {leaked}"

    # Guard the win32 arm the Linux edit sits next to (v2.0.2 WIN-START-FIX).
    assert "clr" in _spec_hiddenimports("win32")


def test_icon_is_three_way_and_linux_never_gets_the_ico():
    """An ELF carries no embedded icon; handing PyInstaller `assets/icon.ico`
    on Linux makes it attempt a Windows resource stamp. The macOS/Windows
    values must be exactly what they are today."""
    if "_LINUX" not in (ROOT / "packaging" / "domestique.spec").read_text(encoding="utf-8"):
        pytest.skip("spec has no Linux arm yet (PACKAGING agent)")

    icon_expr = _spec_call_kwarg("EXE", "icon")
    icons = {p: _spec_eval(icon_expr, p, _spec_ns(p))
             for p in ("darwin", "win32", "linux")}

    assert icons["darwin"] == "../assets/icon.icns"
    assert icons["win32"] == "../assets/icon.ico"
    assert icons["linux"] is None or not str(icons["linux"]).endswith(".ico")
    assert len(set(map(str, icons.values()))) == 3, (
        f"icon expression is not three-way: {icons}")


# ─── 4. launcher.py ──────────────────────────────────────────────────────────

def _launcher_tree() -> ast.Module:
    return ast.parse((ROOT / "src" / "launcher.py").read_text(encoding="utf-8"))


def _env_keys_written(tree: ast.Module) -> list[str]:
    """Every constant env-var name launcher.py writes (assign/setdefault/putenv)."""
    keys: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Attribute)
                        and tgt.value.attr == "environ"
                        and isinstance(tgt.slice, ast.Constant)):
                    keys.append(str(tgt.slice.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr, args = node.func.attr, node.args
            is_env_write = (
                (attr == "setdefault" and isinstance(node.func.value, ast.Attribute)
                 and node.func.value.attr == "environ")
                or attr == "putenv")
            if is_env_write and args and isinstance(args[0], ast.Constant):
                keys.append(str(args[0].value))
    return keys


def test_launcher_sets_no_qt_environment_variables():
    """§9: AppRun is the sole owner of the Qt environment, with append
    semantics. A second writer in launcher.py silently overwrites what the
    shipped AppRun computed, so CI would certify a configuration no user runs."""
    offenders = [k for k in _env_keys_written(_launcher_tree())
                 if k.upper().startswith(("QT", "QTWEBENGINE", "QSG"))]
    assert offenders == [], f"launcher.py must not set Qt env vars: {offenders}"


def _has_linux_guard(tree: ast.Module) -> bool:
    """True once the browser fallback carries its Linux choke point."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_fallback_to_browser"):
            continue
        return any(
            isinstance(sub, ast.If)
            and any(isinstance(c, ast.Constant) and c.value == "linux"
                    for c in ast.walk(sub.test))
            for sub in ast.walk(node))
    return False


def test_linux_backend_failure_is_fatal_not_a_browser_fallback(monkeypatch, capsys):
    """§7, the worst failure mode of this release, driven rather than read.

    Traced in pywebview 6.1: the Qt backend raises WebViewException (not
    ImportError) → the generic handler → the browser fallback, whose message
    box is win32-guarded and whose tray helper needs pystray, unavailable on
    Linux → a live process holding the port with no window, no tray, no error
    and no exit. So the fallback is called here AS a Linux process and must
    exit(1) having touched neither the browser nor the tray.
    """
    import launcher

    if not _has_linux_guard(_launcher_tree()):
        pytest.skip("browser fallback has no Linux guard yet (CODE agent)")

    opened: list = []
    monkeypatch.setattr(launcher, "_open_url", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(launcher.webbrowser, "open",
                        lambda *a, **k: opened.append(a))
    monkeypatch.setattr(launcher, "run_with_tray",
                        lambda *a, **k: opened.append("tray"))
    monkeypatch.setattr(launcher.sys, "platform", "linux")

    with pytest.raises(SystemExit) as exc:
        launcher._fallback_to_browser("pywebview failed: WebViewException")

    assert exc.value.code == 1, "a dead GUI backend must exit non-zero"
    assert opened == [], f"the Linux path reached the browser/tray: {opened}"
    # HOME is the conftest sandbox, so this asserts the real write path without
    # touching the developer's ~/.domestique.
    crash = Path.home() / ".domestique" / "startup_crash.txt"
    assert crash.exists(), "no crash report written — the failure is invisible"
    assert "WebViewException" in crash.read_text(encoding="utf-8")
    assert "could not open its window" in capsys.readouterr().err


def test_browser_fallback_still_works_on_macos_and_windows(monkeypatch):
    """The other half of §7: the Linux guard must be an early return for Linux
    ONLY. macOS and Windows keep the degraded-but-usable browser path they
    ship with today."""
    import launcher

    for plat in ("darwin", "win32"):
        opened: list = []
        monkeypatch.setattr(launcher, "_open_url", lambda u, *a: opened.append(u))
        monkeypatch.setattr(launcher, "run_with_tray",
                            lambda *a: opened.append("tray"))
        monkeypatch.setattr(launcher.sys, "platform", plat)

        launcher._fallback_to_browser("pywebview failed")  # must NOT raise

        assert opened == [launcher.URL, "tray"], f"{plat} fallback changed: {opened}"



# ── the fixes the adversarial verifier forced (B1, H2, M1) ───────────────────

def test_the_crash_dialog_cannot_satisfy_the_native_window_check():
    """B1. The CI smoke test proves a native window exists by searching X for a
    mapped window titled "Domestique". The startup-failure dialog is a real Qt
    window, so if it carried that title the release would go green while the app
    was dying in front of it — every assertion in the job passes off the dialog.
    Its title must be one no success path can emit.

    Reads _fatal_report, which owns the dialog for BOTH startup deaths — the
    dead GUI backend this was written for, and a foreign server holding :8080.
    """
    src = (ROOT / "src" / "launcher.py").read_text(encoding="utf-8")
    i = src.index("def _fatal_report")
    body = src[i:i + 4000]
    assert 'setWindowTitle("Domestique")' not in body, (
        "the fatal dialog is titled exactly like the real window")
    assert "startup failure" in body
    # Both fatal paths must go through it, or one of them is silent again.
    assert src.count("_fatal_report(") >= 3, (
        "a startup death stopped routing through the visible reporter")


def test_a_second_linux_launch_does_not_open_a_browser():
    """H2. Double-clicking the AppImage while it is already running fell through
    to _open_url — a browser tab, which is the one degradation this release
    forbids, reached by the DEFAULT path. Focusing the existing window is out of
    scope; doing nothing is correct, doing the wrong thing is not."""
    src = (ROOT / "src" / "launcher.py").read_text(encoding="utf-8")
    i = src.index("Domestique already running →")
    tail = src[i:i + 700]
    assert 'sys.platform.startswith("linux")' in tail, (
        "the already-running path still opens a browser on Linux")


@pytest.mark.parametrize("orig,ours,expect", [
    ("/usr/lib:/lib", "/bundle/lib", "/usr/lib:/lib"),   # ORIG present → restore
    (None, "/bundle/lib", None),                          # ORIG absent  → unset
])
def test_open_url_restores_the_hosts_library_path(monkeypatch, orig, ours, expect):
    """M1. The bootloader points LD_LIBRARY_PATH at the bundle, and a browser
    spawned with it inherits libraries it must not use — on the one path by
    which a Linux user is offered an update. Untested until now."""
    import sys
    sys.path.insert(0, str(ROOT))
    import launcher

    seen = {}
    monkeypatch.setattr(launcher.webbrowser, "open",
                        lambda u: seen.update(v=os.environ.get("LD_LIBRARY_PATH", "\0")))
    monkeypatch.setenv("LD_LIBRARY_PATH", ours)
    if orig is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", orig)
    launcher._open_url("http://x", platform="linux")
    got = seen.get("v")
    assert got == (expect if expect is not None else "\0"), got
    # and ours is put back afterwards, so the app keeps working
    assert os.environ.get("LD_LIBRARY_PATH") == ours
