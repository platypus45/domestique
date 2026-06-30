"""Release guard: README + cask version must match the VERSION file.

The release flow bumps VERSION / CHANGELOG / cask but historically forgot the
README version badge + "Latest" line, so the README repeatedly shipped a stale
version. This test makes that a hard failure — the suite goes red until the
README badge and the cask both match VERSION, so a release can't leave them
behind.
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


def test_cask_version_matches_version_file():
    ver = _version()
    cask = (ROOT / "Casks" / "domestique.rb").read_text(encoding="utf-8")
    assert f'version "{ver}"' in cask, (
        f'Casks/domestique.rb is stale — expected version "{ver}".'
    )
