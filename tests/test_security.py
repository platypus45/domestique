"""Security regression tests for Domestique.

These lock in the security posture documented in README "Security notes":

  1. Path-traversal guard (`app._safe_path`) — the control behind every file
     download endpoint. resolve() + is_relative_to(base) must reject ../, an
     absolute path, and nested escapes, while allowing legit (possibly nested)
     names.
  2. The download endpoints actually use that guard (no foreign-file leak).
  3. Credential storage — the intervals.icu key writer rejects newline
     injection and writes the .env owner-only (0600).
  4. Localhost-only bind — the launcher binds 127.0.0.1, never 0.0.0.0
     (no remote access). Regression guard on the bind constant.
  5. No inbound-network entitlement in the notarized macOS build.

They are deliberately cheap (no full server lifespan, no network) so they can
run in CI on every change.
"""
import os
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import profile_manager

REPO = Path(__file__).resolve().parent.parent


# ── 1. Path-traversal guard (the control itself) ───────────────────────────────

# Payloads are passed as the routes pass them: each tuple element is one URL
# path segment (a {region}/{filename} route -> two parts; a {filename} route ->
# one part). An absolute path arriving as a single segment is the real attack —
# Path.joinpath() resets to it, so the is_relative_to() check must catch it.
@pytest.mark.parametrize("parts", [
    ("..", "etc", "passwd"),               # region/filename climb-out
    ("..", "..", "..", "..", "etc", "passwd"),
    ("/etc/passwd",),                      # single absolute segment (joinpath resets to abs)
    ("..",),                               # the base's parent
    ("sub", "..", "..", "escape"),         # climb out via a real subdir
    ("foo", "..", "..", "bar"),
])
def test_safe_path_blocks_traversal(tmp_path, parts):
    base = tmp_path / "base"
    base.mkdir()
    assert app_module._safe_path(base, *parts) is None, f"traversal not blocked: {parts!r}"


@pytest.mark.parametrize("ok", ["legit.zwo", "sub/legit.zwo", "a/b/c.crs"])
def test_safe_path_allows_legit_names(tmp_path, ok):
    base = tmp_path / "base"
    base.mkdir()
    resolved = app_module._safe_path(base, *ok.split("/"))
    assert resolved is not None
    assert resolved.resolve().is_relative_to(base.resolve())


# ── 2. Download endpoint refuses to leak a file outside its base ───────────────

def test_download_endpoint_rejects_traversal():
    client = TestClient(app_module.app)
    # encoded ../ climb out of the workout dir toward /etc/passwd
    evil = urllib.parse.quote("../../../../etc/passwd", safe="")
    resp = client.get(f"/api/download/zwo/{evil}")
    assert resp.status_code != 200, "traversal request unexpectedly succeeded"
    assert "root:" not in resp.text, "endpoint leaked /etc/passwd contents"


# ── 3. Credential storage ──────────────────────────────────────────────────────

def _pm_with_tmp_profile(tmp_path):
    """A ProfileManager whose active profile dir is under tmp_path, so save_env
    never touches the real ~/.domestique."""
    pm = profile_manager.ProfileManager()
    pm._profiles_dir = tmp_path / "profiles"
    pm._active_id = "t"
    (pm._profiles_dir / "t").mkdir(parents=True)
    pm._env = {}
    return pm


@pytest.mark.parametrize("field", ["athlete", "key"])
def test_save_env_rejects_newline_injection(tmp_path, field):
    pm = _pm_with_tmp_profile(tmp_path)
    athlete = "i123\nICU_API_KEY=stolen" if field == "athlete" else "i123"
    key = "secret\nEXTRA=evil" if field == "key" else "secret"
    with pytest.raises(ValueError):
        pm.save_env(athlete, key)


def test_save_env_writes_owner_only_0600(tmp_path):
    pm = _pm_with_tmp_profile(tmp_path)
    pm.save_env("i123", "SECRETKEY")
    env_file = pm._profiles_dir / "t" / ".env"
    assert env_file.exists()
    mode = oct(env_file.stat().st_mode)[-3:]
    assert mode == "600", f"expected 0600, got {mode}"
    body = env_file.read_text()
    assert "ICU_API_KEY=SECRETKEY" in body
    # no injected extra lines — exactly the three known keys, nothing else
    # (ICU_ACCESS_TOKEN is always written since the per-profile OAuth change;
    # empty here because no OAuth token is stored).
    assert [ln for ln in body.splitlines() if ln.strip()] == [
        "ICU_ATHLETE_ID=i123",
        "ICU_API_KEY=SECRETKEY",
        "ICU_ACCESS_TOKEN=",
    ]


# ── 4. Localhost-only bind (no remote access) ──────────────────────────────────

def test_launcher_binds_localhost_only():
    src = (REPO / "launcher.py").read_text()
    assert '"127.0.0.1"' in src, "launcher should bind 127.0.0.1"
    assert "0.0.0.0" not in src, "launcher must never bind 0.0.0.0 (would expose the API to the network)"


def test_no_python_module_binds_all_interfaces():
    for py in REPO.glob("*.py"):
        assert "0.0.0.0" not in py.read_text(), f"{py.name} contains a 0.0.0.0 bind"


# ── 5. macOS build ships no inbound-network entitlement ────────────────────────

def test_no_inbound_network_server_entitlement():
    ent = REPO / "entitlements.plist"
    if not ent.exists():
        pytest.skip("no entitlements.plist")
    text = ent.read_text()
    assert "network.server" not in text, "inbound network.server entitlement would allow remote access"
