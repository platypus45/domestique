"""Release guard: the README version badge must match the VERSION file.

The release flow bumps VERSION / CHANGELOG / cask but historically forgot the
README version badge + "Latest" line, so the README repeatedly shipped a stale
version. This test makes that a hard failure — the suite goes red until the
README badge matches VERSION, so a release can't leave it behind.

(The cask version is deliberately NOT asserted here: the cask is bumped LAST in
the release flow — after the DMG SHA exists — so during a release the cask lags
VERSION by design, and asserting it would false-fail the pre-release gate.)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_readme_version_badge_matches_version_file():
    ver = _version()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Version-v{ver}-" in readme, (
        f"README version badge is stale — expected the shields.io badge to read "
        f"v{ver} (matching the VERSION file). Bump it on every release."
    )
