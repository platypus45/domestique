"""v1.6.2 — delete-protection guard.

After v1.6.2 every write to ``current_plan.json`` MUST go through
``training_planner.atomic_write_plan`` (which rotates backups and only
performs an atomic ``Path.replace``). No source file outside the
sanctioned helpers may call ``.unlink()`` or ``os.remove()`` on a path
referencing the live plan file.

Allowed sites:

* ``training_planner._rotate_plan_backups`` — drops the oldest .bak
  during rotation. Sanctioned by design.
* (No other call site is allowed to call unlink/remove on the plan
  path. The boot-time auto-restore writes through tmp + replace, never
  unlink-then-write.)

This test scans the source. It is intentionally strict: any new direct
``.unlink()`` on a plan-path variable will fail it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("app.py", "training_planner.py")

# Variable names commonly bound to the live plan file.
PLAN_VAR_RE = re.compile(
    r"\b(?:json_path|plan_path|_plan_json|_plan_path|current_plan)\b"
)
UNLINK_RE = re.compile(r"\.unlink\s*\(\s*\)")
REMOVE_RE = re.compile(r"os\.remove\s*\(")
ALLOWED_HELPERS = {"_rotate_plan_backups"}


def _function_at_line(lines: list[str], lineno: int) -> str:
    """Return the name of the function/method enclosing 1-indexed lineno."""
    for i in range(lineno - 1, -1, -1):
        line = lines[i]
        m = re.match(r"\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m:
            return m.group(1)
    return "<module>"


def _scan(src_file: Path) -> list[tuple[int, str, str]]:
    """Return ``[(lineno, line, enclosing_function), ...]`` for every
    .unlink() or os.remove() call on a plan-path variable in ``src_file``.
    """
    lines = src_file.read_text(encoding="utf-8").splitlines()
    hits: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines, start=1):
        # Strip end-of-line comments to avoid matching docstring narrative.
        code = line.split("#", 1)[0]
        if not (UNLINK_RE.search(code) or REMOVE_RE.search(code)):
            continue
        if not PLAN_VAR_RE.search(code):
            continue
        fn = _function_at_line(lines, idx)
        hits.append((idx, line.rstrip(), fn))
    return hits


def test_no_unlink_on_plan_path_outside_sanctioned_helpers() -> None:
    bad: list[str] = []
    for fname in SOURCES:
        path = REPO_ROOT / fname
        for lineno, line, fn in _scan(path):
            if fn in ALLOWED_HELPERS:
                continue
            bad.append(f"{fname}:{lineno} ({fn}): {line.strip()}")

    assert not bad, (
        "v1.6.2 forbids direct .unlink()/os.remove() on the plan path "
        "outside the sanctioned helpers (_rotate_plan_backups). All plan "
        "writes must go through training_planner.atomic_write_plan. "
        "Offending sites:\n  " + "\n  ".join(bad)
    )


def test_atomic_write_plan_helper_is_used() -> None:
    """app.py should call ``tp.atomic_write_plan`` directly — the inline
    tmp+rename pattern is gone."""
    app_src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "tp.atomic_write_plan" in app_src, (
        "app.py must call tp.atomic_write_plan after v1.6.2 migration"
    )
    # Stricter: no inline tmp_path = json_path.with_suffix('.tmp') pattern
    # remains. (One per legacy site; the regex catches them all.)
    inline_pat = re.compile(
        r"tmp_path\s*=\s*json_path\.with_suffix\(['\"]\.tmp['\"]\)"
    )
    leftover = inline_pat.findall(app_src)
    assert not leftover, (
        f"app.py still has {len(leftover)} inline tmp+rename plan-write "
        "blocks; replace each with tp.atomic_write_plan(json_path, plan)."
    )
