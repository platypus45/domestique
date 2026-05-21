"""v1.0.2 IMPL-BANNER + v1.8.7 expand — /api/update/check contract tests (MASTER §1).

Locked response shape (must not regress):
  current, latest, update_available, release_url, download_url,
  asset_name, platform, checked_at, cached, error, release_body.

Tests cover:
  1. Cache hit (fresh cache returns cached=true without calling GitHub).
  2. Cache miss + API success (writes cache, returns cached=false).
  3. Cache miss + API failure (returns last-good cache, error populated).
  4. Platform asset filtering (darwin → .dmg, win32 → .exe/.zip).
  5. release_body passthrough, truncation, null-on-failure, cache persistence.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


def _fake_release(tag: str = "9.9.9", assets: list | None = None,
                  body: str | None = None) -> dict:
    """Synthesised GitHub Releases API payload."""
    rel = {
        "tag_name": f"v{tag}",
        "html_url": f"https://github.com/platypus45/domestique/releases/tag/v{tag}",
        "assets": assets if assets is not None else [
            {"name": "Domestique.dmg",
             "browser_download_url": f"https://example.com/Domestique-{tag}.dmg"},
            {"name": "Domestique.exe",
             "browser_download_url": f"https://example.com/Domestique-{tag}.exe"},
        ],
    }
    if body is not None:
        rel["body"] = body
    return rel


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class UpdateCheckBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._cache_path = self._tmp / "update_check_cache.json"
        # Redirect the cache file to a tmp path for every test.
        self._patch_cache = patch.object(
            app_module, "_update_check_cache_path", return_value=self._cache_path
        )
        self._patch_cache.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_cache.stop()
        self._tmpdir.cleanup()

    def _write_cache(self, payload: dict, age_seconds: int = 0):
        body = dict(payload)
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        body["cache_written_at"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._cache_path.write_text(json.dumps(body), encoding="utf-8")


class TestCacheHit(UpdateCheckBase):
    def test_fresh_cache_returns_without_calling_github(self):
        """A cache file written < 6h ago is returned with cached=true."""
        self._write_cache({
            "current": "1.0.0",
            "latest": "1.0.5",
            "update_available": True,
            "release_url": "https://example.com/release",
            "download_url": "https://example.com/Domestique.dmg",
            "asset_name": "Domestique.dmg",
            "platform": "darwin",
            "checked_at": "2026-05-01T00:00:00Z",
            "error": None,
            "release_body": "## Header\n- cached bullet",
        }, age_seconds=60)  # 1 minute old → fresh

        # If GitHub is hit, the test fails: any httpx.get raises.
        def _boom(*a, **kw):
            raise AssertionError("httpx.get should not be called when cache is fresh")
        with patch("httpx.get", side_effect=_boom):
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["cached"])
        self.assertEqual(data["latest"], "1.0.5")
        self.assertIsNone(data["error"])
        self.assertEqual(data["release_body"], "## Header\n- cached bullet")
        # Locked field set must be present (no extras dropped).
        for k in ("current", "latest", "update_available", "release_url",
                  "download_url", "asset_name", "platform", "checked_at",
                  "cached", "error", "release_body"):
            self.assertIn(k, data, f"missing locked field: {k}")


class TestCacheMissApiSuccess(UpdateCheckBase):
    def test_cache_miss_hits_api_and_writes_cache(self):
        """No cache + 200 OK from GitHub: returns cached=false, persists cache."""
        self.assertFalse(self._cache_path.exists())
        rel = _fake_release(tag="9.9.9")
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "darwin"
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertFalse(data["cached"])
        self.assertEqual(data["latest"], "9.9.9")
        self.assertTrue(data["update_available"])  # current=1.0.0 < 9.9.9
        self.assertEqual(data["asset_name"], "Domestique.dmg")
        self.assertTrue(data["download_url"].endswith(".dmg"))
        self.assertEqual(data["platform"], "darwin")
        self.assertIsNone(data["error"])
        # Cache file written, contains cache_written_at + the response.
        self.assertTrue(self._cache_path.exists())
        cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["latest"], "9.9.9")
        self.assertIn("cache_written_at", cached)


class TestCacheMissApiFailure(UpdateCheckBase):
    def test_api_failure_returns_last_good_cache_with_error(self):
        """Stale cache + httpx error → returns cached=true with error populated."""
        # Pre-existing cache, but stale (older than 6h TTL).
        self._write_cache({
            "current": "1.0.0",
            "latest": "1.0.4",
            "update_available": True,
            "release_url": "https://example.com/old-release",
            "download_url": "https://example.com/Old.dmg",
            "asset_name": "Old.dmg",
            "platform": "darwin",
            "checked_at": "2025-12-01T00:00:00Z",
            "error": None,
        }, age_seconds=24 * 60 * 60)  # 24h old → stale

        def _raise(*a, **kw):
            import httpx
            raise httpx.ConnectError("simulated network failure")

        with patch("httpx.get", side_effect=_raise):
            r = self.client.get("/api/update/check")

        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Falls back to last-good cache.
        self.assertEqual(data["latest"], "1.0.4")
        self.assertEqual(data["asset_name"], "Old.dmg")
        self.assertTrue(data["cached"])
        # error is populated with the failure reason.
        self.assertIsNotNone(data["error"])
        self.assertIn("simulated network failure", data["error"])

    def test_api_failure_with_no_cache_returns_minimal_shape(self):
        """No cache + httpx error → minimal shape with error, all locked fields present."""
        self.assertFalse(self._cache_path.exists())
        with patch("httpx.get", side_effect=RuntimeError("boom")):
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIsNone(data["latest"])
        self.assertFalse(data["update_available"])
        self.assertIsNone(data["download_url"])
        self.assertIsNotNone(data["error"])
        for k in ("current", "latest", "update_available", "release_url",
                  "download_url", "asset_name", "platform", "checked_at",
                  "cached", "error", "release_body"):
            self.assertIn(k, data)


class TestPlatformAssetFiltering(UpdateCheckBase):
    def test_darwin_picks_dmg(self):
        rel = _fake_release(tag="2.3.4", assets=[
            {"name": "Domestique.dmg",
             "browser_download_url": "https://example.com/Domestique.dmg"},
            {"name": "Domestique.exe",
             "browser_download_url": "https://example.com/Domestique.exe"},
            {"name": "Domestique.zip",
             "browser_download_url": "https://example.com/Domestique.zip"},
        ])
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "darwin"
            r = self.client.get("/api/update/check")
        data = r.json()
        self.assertEqual(data["asset_name"], "Domestique.dmg")
        self.assertEqual(data["download_url"], "https://example.com/Domestique.dmg")
        self.assertEqual(data["platform"], "darwin")

    def test_darwin_prefers_canonical_dmg_over_decorated(self):
        # Multiple .dmg assets — the unadorned "Domestique.dmg" must win.
        rel = _fake_release(tag="2.3.4", assets=[
            {"name": "Domestique-2.3.4.dmg",
             "browser_download_url": "https://example.com/Domestique-2.3.4.dmg"},
            {"name": "Domestique.dmg",
             "browser_download_url": "https://example.com/Domestique.dmg"},
        ])
        # Cache must be empty so we hit the API path.
        if self._cache_path.exists():
            self._cache_path.unlink()
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "darwin"
            r = self.client.get("/api/update/check")
        data = r.json()
        self.assertEqual(data["asset_name"], "Domestique.dmg")

    def test_win32_prefers_exe_over_zip(self):
        rel = _fake_release(tag="2.3.4", assets=[
            {"name": "Domestique.zip",
             "browser_download_url": "https://example.com/Domestique.zip"},
            {"name": "Domestique.exe",
             "browser_download_url": "https://example.com/Domestique.exe"},
            {"name": "Domestique.dmg",
             "browser_download_url": "https://example.com/Domestique.dmg"},
        ])
        if self._cache_path.exists():
            self._cache_path.unlink()
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "win32"
            r = self.client.get("/api/update/check")
        data = r.json()
        self.assertEqual(data["asset_name"], "Domestique.exe")
        self.assertEqual(data["download_url"], "https://example.com/Domestique.exe")
        self.assertEqual(data["platform"], "win32")

    def test_win32_falls_back_to_zip_when_no_exe(self):
        rel = _fake_release(tag="2.3.4", assets=[
            {"name": "Domestique.zip",
             "browser_download_url": "https://example.com/Domestique.zip"},
            {"name": "Domestique.dmg",
             "browser_download_url": "https://example.com/Domestique.dmg"},
        ])
        if self._cache_path.exists():
            self._cache_path.unlink()
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "win32"
            r = self.client.get("/api/update/check")
        data = r.json()
        self.assertEqual(data["asset_name"], "Domestique.zip")

    def test_linux_returns_release_url_only(self):
        rel = _fake_release(tag="2.3.4")
        if self._cache_path.exists():
            self._cache_path.unlink()
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "linux"
            r = self.client.get("/api/update/check")
        data = r.json()
        self.assertIsNone(data["download_url"])
        self.assertIsNone(data["asset_name"])
        # release_url is still populated so the user can navigate to GitHub.
        self.assertTrue(data["release_url"].endswith("/v2.3.4"))


class TestReleaseBody(UpdateCheckBase):
    """v1.8.7 — release_body field (MASTER §5)."""

    def test_release_body_passthrough(self):
        """GitHub returns body markdown → endpoint surfaces it unmodified."""
        rel = _fake_release(tag="9.9.9", body="## Header\n- bullet")
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "darwin"
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["release_body"], "## Header\n- bullet")

    def test_release_body_truncation(self):
        """Body > 8192 chars is truncated to 8192 + suffix appended server-side."""
        huge = "x" * 20000
        rel = _fake_release(tag="9.9.9", body=huge)
        with patch("httpx.get", return_value=_FakeResp(200, rel)), \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "darwin"
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        body = data["release_body"]
        self.assertIsNotNone(body)
        suffix = "\n\n… (full release notes on GitHub)"
        # 8192 chars of raw body then the suffix appended.
        self.assertEqual(len(body), 8192 + len(suffix))
        self.assertTrue(body.endswith(suffix))
        # First 8192 chars are the raw body content.
        self.assertEqual(body[:8192], "x" * 8192)

    def test_release_body_null_on_failure_no_cache(self):
        """GitHub call fails AND no cache file → release_body is None."""
        self.assertFalse(self._cache_path.exists())
        with patch("httpx.get", side_effect=RuntimeError("boom")):
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIsNone(data["release_body"])
        self.assertIsNotNone(data["error"])

    def test_release_body_persisted_in_cache(self):
        """First call writes body to cache; second call within TTL returns same body from cache."""
        rel = _fake_release(tag="9.9.9", body="## v9.9.9\n- shipped notarized DMG")
        # First call hits API and writes cache.
        with patch("httpx.get", return_value=_FakeResp(200, rel)) as mock_get, \
             patch.object(app_module, "sys") as _sys:
            _sys.platform = "darwin"
            r1 = self.client.get("/api/update/check")
            self.assertEqual(mock_get.call_count, 1)
        data1 = r1.json()
        self.assertFalse(data1["cached"])
        self.assertEqual(data1["release_body"], "## v9.9.9\n- shipped notarized DMG")
        # Cache file must contain the body.
        self.assertTrue(self._cache_path.exists())
        on_disk = json.loads(self._cache_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["release_body"], "## v9.9.9\n- shipped notarized DMG")
        # Second call within TTL must NOT call httpx and must return the same body.
        def _boom(*a, **kw):
            raise AssertionError("httpx.get should not be called when cache is fresh")
        with patch("httpx.get", side_effect=_boom):
            r2 = self.client.get("/api/update/check")
        data2 = r2.json()
        self.assertTrue(data2["cached"])
        self.assertEqual(data2["release_body"], "## v9.9.9\n- shipped notarized DMG")


if __name__ == "__main__":
    unittest.main()
