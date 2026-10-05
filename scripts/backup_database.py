from __future__ import annotations

import argparse
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup(source: Path, target_dir: Path, keep: int) -> Path:
    source = source.resolve(strict=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = (target_dir / f"electrotrace-{stamp}.db").resolve()
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("备份数据库完整性检查失败")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".db.sha256").write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    backups = sorted(target_dir.glob("electrotrace-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    for expired in backups[max(keep, 1):]:
        expired.unlink()
        checksum = expired.with_suffix(".db.sha256")
        if checksum.exists():
            checksum.unlink()
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a consistent ElectroTrace SQLite backup")
    parser.add_argument("--database", type=Path, default=Path("data/electrotrace.db"))
    parser.add_argument("--target", type=Path, default=Path("backups"))
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    print(backup(args.database, args.target, args.keep))
