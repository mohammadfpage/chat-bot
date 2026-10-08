"""One-command verification for the whole project.

    venv\\Scripts\\python smoke\\verify.py

Steps, in order:
  1. compileall (repo, venv excluded)
  2. import bot
  3. every smoke_*.py in this directory

Exit code 0 only when every step passes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SMOKES = [
    "smoke_handlers5.py",
    "smoke_forcejoin.py",
    "smoke_phase2.py",
    "smoke_economy.py",
    "smoke_gift.py",
    "smoke_force_join4.py",
    "smoke_phase6.py",
]


def run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def last_line(out: str) -> str:
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else "(no output)"


def main() -> int:
    failed: list[str] = []

    r = run([PY, "-m", "compileall", "-q", "-x", "venv", str(ROOT)])
    ok = r.returncode == 0
    print(f"compileall:          {'OK' if ok else 'FAIL'}")
    if not ok:
        failed.append("compileall")
        print((r.stdout or "") + (r.stderr or ""))

    r = run([PY, "-c", "import bot"])
    ok = r.returncode == 0
    print(f"import bot:          {'OK' if ok else 'FAIL'}")
    if not ok:
        failed.append("import bot")
        print((r.stdout or "") + (r.stderr or ""))

    for name in SMOKES:
        r = run([PY, str(HERE / name)])
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0
        print(f"{name:<20} {'OK  ' if ok else 'FAIL'} — {last_line(out)[:80]}")
        if not ok:
            failed.append(name)
            print(out)

    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"ALL {2 + len(SMOKES)} STEPS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
