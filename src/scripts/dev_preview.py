"""3.4.3 dev-preview sandbox launcher (chip task_23a70236).

Seeds a scratch copy of ~/.domestique ONCE (delete the sandbox dir to
re-seed), sets DOMESTIQUE_HOME at it, and execs uvicorn. The packaged app
never sets DOMESTIQUE_HOME → real home, unchanged. Python (not bash)
because the preview harness only permits python launchers.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sandbox = Path(tempfile.gettempdir()) / "domestique-dev-home"
real = Path.home() / ".domestique"
if not sandbox.exists():
    if real.exists():
        shutil.copytree(real, sandbox,
                        ignore=shutil.ignore_patterns(".backfill.lock"))
        print(f"[dev_preview] seeded sandbox at {sandbox} from {real}")
    else:
        sandbox.mkdir(parents=True)
        print(f"[dev_preview] fresh empty sandbox at {sandbox}")
else:
    print(f"[dev_preview] reusing sandbox at {sandbox} (delete it to re-seed)")

os.environ["DOMESTIQUE_HOME"] = str(sandbox)
os.execvp(sys.executable,
          [sys.executable, "-m", "uvicorn", "app:app", "--port", "8090",
           "--app-dir", "src"])  # v3.11.1 layout: modules live in src/
