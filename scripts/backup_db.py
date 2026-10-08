"""
Online SQLite backup — safe to run while the bot is live (WAL included).

Usage:
    python scripts/backup_db.py            # one snapshot into backups/
    python scripts/backup_db.py --keep 7   # override rotation depth
    python scripts/backup_db.py --out /path/to/dir

Uses the sqlite3 backup API (``Connection.backup``), which copies a
consistent snapshot page-by-page without blocking writers for the whole
copy — unlike ``shutil.copy`` on a WAL database, which can capture a torn
file. The result is a standalone .db, restorable with any sqlite tool.

The admin panel's «💾 پشتیبان‌گیری» button calls ``run_backup`` directly,
so cron, systemd timers and the panel all rotate through the same code.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE_DIR / "database.db"
DEFAULT_OUT = BASE_DIR / "backups"
DEFAULT_KEEP = 14


def run_backup(db_path: Path = DEFAULT_DB, out_dir: Path = DEFAULT_OUT,
               keep: int = DEFAULT_KEEP) -> Path:
    """Snapshot ``db_path`` into ``out_dir``, prune old snapshots, return path."""
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"database-{stamp}.db"
    # Second-resolution stamps collide when several backups land in the same
    # second (a smoke run, a retry loop) — suffix instead of overwriting, or
    # rotation would count one file where two were written.
    if dest.exists():
        n = 1
        while (alt := out_dir / f"database-{stamp}-{n}.db").exists():
            n += 1
        dest = alt

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)  # includes the WAL — one consistent snapshot
        finally:
            dst.close()
    finally:
        src.close()

    # Rotation: keep the newest `keep` snapshots (name sort == time sort,
    # the stamp is lexicographic by design).
    snapshots = sorted(out_dir.glob("database-*.db"))
    for stale in snapshots[:-keep] if keep > 0 else []:
        stale.unlink(missing_ok=True)

    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up database.db")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    args = parser.parse_args(argv)

    try:
        dest = run_backup(args.db, args.out, args.keep)
    except Exception as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1

    size_kb = dest.stat().st_size // 1024
    print(f"backup written: {dest} ({size_kb} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
