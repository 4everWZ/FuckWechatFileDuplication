#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fuck WeChat File Duplication

Aggressive, incremental WeChat file deduplication for Windows/NTFS.
It replaces duplicate files with hard links, so every original path remains valid
while duplicated content occupies disk space only once.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:
    import xxhash  # type: ignore
except ImportError as exc:  # pragma: no cover - clear runtime failure for users
    raise SystemExit(
        "Missing dependency: xxhash\n"
        "Install it with:\n"
        "  python -m pip install -r requirements.txt\n"
        "or:\n"
        "  python -m pip install xxhash\n"
    ) from exc

APP_NAME = "Fuck_Wechat_File_Duplication"
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DEFAULT_CONFIG = {
    "roots": [r"D:\\xwechat_files"],
    "db_path": "wechat_dedupe_index.sqlite3",
    "log_dir": "logs",
    "min_size_bytes": 65536,
    "skip_recent_hours": 72,
    "recent_months": 3,
    "monthly_full_scan_day": 1,
    "process_non_month_dirs": True,
    "aggressive_all_files": True,
    "exclude_dir_names": [],
    "exclude_file_extensions": [],
    "kill_wechat_before_run": False,
    "dry_run": False,
    "verify_before_link": True,
    "same_volume_only": True,
    "hash_buffer_mb": 8,
    "prune_missing_on_full_scan": True,
}

WECHAT_PROCESS_NAMES = [
    "WeChat.exe",
    "WeChatApp.exe",
    "WeChatOCR.exe",
    "WeChatUtility.exe",
    "WeChatPlayer.exe",
    "WeChatBrowser.exe",
]


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    mtime_ns: int
    st_dev: int
    st_ino: int


@dataclass
class Stats:
    scanned_files: int = 0
    eligible_files: int = 0
    indexed_from_db: int = 0
    hashed_files: int = 0
    hardlinked_files: int = 0
    skipped_recent: int = 0
    skipped_small: int = 0
    skipped_locked: int = 0
    skipped_errors: int = 0
    saved_bytes: int = 0


class DedupeDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS file_index (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                hash TEXT NOT NULL,
                st_dev INTEGER,
                st_ino INTEGER,
                updated_at REAL NOT NULL,
                alive INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_file_index_hash_size
                ON file_index(hash, size);
            CREATE INDEX IF NOT EXISTS idx_file_index_size
                ON file_index(size);
            """
        )
        self.conn.commit()

    def get_if_unchanged(self, path: Path, size: int, mtime_ns: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT hash FROM file_index WHERE path=? AND size=? AND mtime_ns=? AND alive=1",
            (str(path), size, mtime_ns),
        ).fetchone()
        return row[0] if row else None

    def upsert(self, record: FileRecord, digest: str) -> None:
        self.conn.execute(
            """
            INSERT INTO file_index(path, size, mtime_ns, hash, st_dev, st_ino, updated_at, alive)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                hash=excluded.hash,
                st_dev=excluded.st_dev,
                st_ino=excluded.st_ino,
                updated_at=excluded.updated_at,
                alive=1
            """,
            (
                str(record.path),
                record.size,
                record.mtime_ns,
                digest,
                record.st_dev,
                record.st_ino,
                time.time(),
            ),
        )

    def candidates_for_hash(self, digest: str, size: int) -> List[Path]:
        rows = self.conn.execute(
            """
            SELECT path FROM file_index
            WHERE hash=? AND size=? AND alive=1
            ORDER BY updated_at ASC
            """,
            (digest, size),
        ).fetchall()
        return [Path(row[0]) for row in rows]

    def mark_missing_under_roots(self, roots: Sequence[Path]) -> int:
        rows = self.conn.execute("SELECT path FROM file_index WHERE alive=1").fetchall()
        missing = []
        root_strs = [str(r.resolve()).lower() for r in roots if r.exists()]
        for (path_str,) in rows:
            p = Path(path_str)
            lower = str(p).lower()
            if root_strs and not any(lower.startswith(rs) for rs in root_strs):
                continue
            if not p.exists():
                missing.append(path_str)
        if missing:
            self.conn.executemany(
                "UPDATE file_index SET alive=0, updated_at=? WHERE path=?",
                [(time.time(), p) for p in missing],
            )
            self.conn.commit()
        return len(missing)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def load_config(path: Path) -> Dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    return cfg


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"wechat_dedupe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fmt = "%(asctime)s | %(levelname)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.info("Log file: %s", log_file)


def kill_wechat_processes() -> None:
    if os.name != "nt":
        logging.warning("Process killing is Windows-only. Skipped on this OS.")
        return
    for proc in WECHAT_PROCESS_NAMES:
        subprocess.run(
            ["taskkill", "/F", "/IM", proc],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    logging.info("Requested WeChat process termination.")


def parse_month_name(name: str) -> Optional[Tuple[int, int]]:
    if not MONTH_RE.match(name):
        return None
    year, month = name.split("-", 1)
    m = int(month)
    if not 1 <= m <= 12:
        return None
    return int(year), m


def month_index(year: int, month: int) -> int:
    return year * 12 + month


def recent_month_threshold(recent_months: int) -> int:
    now = datetime.now()
    return month_index(now.year, now.month) - max(recent_months, 1) + 1


def should_do_scheduled_full_scan(cfg: Dict) -> bool:
    day = int(cfg.get("monthly_full_scan_day", 1))
    return datetime.now().day == day


def is_same_volume(a: Path, b: Path) -> bool:
    if os.name != "nt":
        # POSIX hard links require same device; caller also checks st_dev where possible.
        try:
            return a.stat().st_dev == b.stat().st_dev
        except OSError:
            return False
    drive_a = os.path.splitdrive(str(a.resolve()))[0].lower()
    drive_b = os.path.splitdrive(str(b.resolve()))[0].lower()
    return drive_a == drive_b


def is_same_physical_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def normalize_extensions(exts: Iterable[str]) -> set[str]:
    out = set()
    for e in exts:
        e = str(e).lower().strip()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return out


def iter_files(
    roots: Sequence[Path],
    cfg: Dict,
    full_scan: bool,
    stats: Stats,
) -> Iterator[FileRecord]:
    min_size = int(cfg["min_size_bytes"])
    skip_recent_seconds = float(cfg["skip_recent_hours"]) * 3600.0
    cutoff_mtime = time.time() - skip_recent_seconds
    exclude_dirs = {str(x).lower() for x in cfg.get("exclude_dir_names", [])}
    exclude_exts = normalize_extensions(cfg.get("exclude_file_extensions", []))
    threshold = recent_month_threshold(int(cfg["recent_months"]))
    process_non_month_dirs = bool(cfg.get("process_non_month_dirs", True))

    for root in roots:
        if not root.exists():
            logging.warning("Root does not exist, skipped: %s", root)
            continue
        logging.info("Walking root: %s", root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)

            kept_dirs = []
            for dirname in dirnames:
                lower = dirname.lower()
                if lower in exclude_dirs:
                    continue
                parsed = parse_month_name(dirname)
                if parsed and not full_scan:
                    y, m = parsed
                    if month_index(y, m) < threshold:
                        continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            current_month = parse_month_name(current.name)
            if current_month and not full_scan:
                y, m = current_month
                if month_index(y, m) < threshold:
                    continue

            if not process_non_month_dirs:
                # Only allow files directly inside selected YYYY-MM directories or their children.
                parts = current.parts
                if not any(parse_month_name(part) for part in parts):
                    continue

            for filename in filenames:
                stats.scanned_files += 1
                p = current / filename
                try:
                    if exclude_exts and p.suffix.lower() in exclude_exts:
                        continue
                    if p.is_symlink():
                        continue
                    st = p.stat()
                    if not os.path.isfile(p):
                        continue
                    if st.st_size < min_size:
                        stats.skipped_small += 1
                        continue
                    if st.st_mtime > cutoff_mtime:
                        stats.skipped_recent += 1
                        continue
                    stats.eligible_files += 1
                    yield FileRecord(
                        path=p,
                        size=int(st.st_size),
                        mtime_ns=int(st.st_mtime_ns),
                        st_dev=int(getattr(st, "st_dev", 0)),
                        st_ino=int(getattr(st, "st_ino", 0)),
                    )
                except PermissionError:
                    stats.skipped_locked += 1
                    logging.debug("Permission denied: %s", p)
                except OSError as exc:
                    stats.skipped_errors += 1
                    logging.debug("Stat failed: %s | %s", p, exc)


def hash_file(path: Path, buffer_size: int) -> str:
    h = xxhash.xxh3_128()
    with path.open("rb", buffering=0) as f:
        while True:
            chunk = f.read(buffer_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def stat_record(path: Path) -> Optional[FileRecord]:
    try:
        st = path.stat()
        if not os.path.isfile(path):
            return None
        return FileRecord(
            path=path,
            size=int(st.st_size),
            mtime_ns=int(st.st_mtime_ns),
            st_dev=int(getattr(st, "st_dev", 0)),
            st_ino=int(getattr(st, "st_ino", 0)),
        )
    except OSError:
        return None


def select_canonical(
    current: FileRecord,
    candidates: Sequence[Path],
    cfg: Dict,
) -> Optional[Path]:
    for candidate in candidates:
        if str(candidate) == str(current.path):
            continue
        if not candidate.exists():
            continue
        if cfg.get("same_volume_only", True) and not is_same_volume(current.path, candidate):
            continue
        if is_same_physical_file(current.path, candidate):
            return None
        cand_rec = stat_record(candidate)
        if cand_rec is None:
            continue
        if cand_rec.size != current.size:
            continue
        return candidate
    return None


def hardlink_duplicate(
    duplicate: FileRecord,
    canonical: Path,
    cfg: Dict,
    dry_run: bool,
) -> Tuple[bool, Optional[FileRecord]]:
    if cfg.get("verify_before_link", True):
        latest = stat_record(duplicate.path)
        if latest is None:
            logging.debug("Duplicate disappeared before link: %s", duplicate.path)
            return False, None
        if latest.size != duplicate.size or latest.mtime_ns != duplicate.mtime_ns:
            logging.debug("Duplicate changed before link, skipped: %s", duplicate.path)
            return False, latest

    if dry_run:
        logging.info("[DRY-RUN] duplicate -> hardlink: %s -> %s", duplicate.path, canonical)
        return True, duplicate

    tmp_backup = duplicate.path.with_name(
        f".{duplicate.path.name}.dedupe_backup.{os.getpid()}.{int(time.time() * 1000)}"
    )

    try:
        duplicate.path.rename(tmp_backup)
        try:
            os.link(str(canonical), str(duplicate.path))
        except OSError:
            # Restore original path if hardlink creation fails.
            tmp_backup.rename(duplicate.path)
            raise

        try:
            tmp_backup.unlink()
        except OSError as exc:
            logging.warning("Hardlink created but backup cleanup failed: %s | %s", tmp_backup, exc)

        linked = stat_record(duplicate.path)
        logging.info("hardlinked: %s -> %s", duplicate.path, canonical)
        return True, linked
    except OSError as exc:
        logging.warning("Hardlink failed: %s -> %s | %s", duplicate.path, canonical, exc)
        return False, stat_record(duplicate.path)


def process_records(records: Sequence[FileRecord], db: DedupeDB, cfg: Dict, stats: Stats, dry_run: bool) -> None:
    buffer_size = max(1, int(cfg.get("hash_buffer_mb", 8))) * 1024 * 1024
    db_cache: Dict[str, str] = {}

    for i, rec in enumerate(records, start=1):
        if i % 1000 == 0:
            logging.info(
                "Progress: %d/%d | hashed=%d | linked=%d | saved=%.2f MB",
                i,
                len(records),
                stats.hashed_files,
                stats.hardlinked_files,
                stats.saved_bytes / 1024 / 1024,
            )
            db.commit()

        cached = db.get_if_unchanged(rec.path, rec.size, rec.mtime_ns)
        if cached:
            digest = cached
            stats.indexed_from_db += 1
        else:
            try:
                before = stat_record(rec.path)
                if before is None:
                    continue
                digest = hash_file(rec.path, buffer_size)
                after = stat_record(rec.path)
                if after is None:
                    continue
                if after.size != before.size or after.mtime_ns != before.mtime_ns:
                    logging.debug("File changed while hashing, skipped this round: %s", rec.path)
                    continue
                stats.hashed_files += 1
                db.upsert(after, digest)
            except PermissionError:
                stats.skipped_locked += 1
                continue
            except OSError as exc:
                stats.skipped_errors += 1
                logging.debug("Hash failed: %s | %s", rec.path, exc)
                continue

        candidates = db.candidates_for_hash(digest, rec.size)
        canonical = select_canonical(rec, candidates, cfg)
        if canonical is None:
            db.upsert(rec, digest)
            continue

        ok, linked_record = hardlink_duplicate(rec, canonical, cfg, dry_run=dry_run)
        if ok:
            stats.hardlinked_files += 1
            stats.saved_bytes += rec.size
            if linked_record is not None:
                db.upsert(linked_record, digest)
        else:
            db.upsert(rec, digest)

    db.commit()


def write_default_config(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aggressive incremental WeChat file deduplication with xxhash + hardlinks.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--full", action="store_true", help="Scan all month folders, not only recent months.")
    parser.add_argument("--dry-run", action="store_true", help="Only report planned actions; do not hardlink.")
    parser.add_argument("--init-config", action="store_true", help="Write default config.json if missing, then exit.")
    parser.add_argument("--kill-wechat", action="store_true", help="Kill WeChat processes before scanning.")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = script_dir / config_path

    if args.init_config:
        write_default_config(config_path)
        print(f"Config ready: {config_path}")
        return 0

    cfg = load_config(config_path)
    roots = [Path(p).expanduser() for p in cfg.get("roots", [])]

    db_path = Path(cfg.get("db_path", DEFAULT_CONFIG["db_path"]))
    if not db_path.is_absolute():
        db_path = script_dir / db_path

    log_dir = Path(cfg.get("log_dir", DEFAULT_CONFIG["log_dir"]))
    if not log_dir.is_absolute():
        log_dir = script_dir / log_dir
    setup_logging(log_dir)

    if args.kill_wechat or cfg.get("kill_wechat_before_run", False):
        kill_wechat_processes()
        time.sleep(2)

    dry_run = bool(args.dry_run or cfg.get("dry_run", False))
    db_missing_before_run = not db_path.exists()
    scheduled_full = should_do_scheduled_full_scan(cfg)
    full_scan = bool(args.full or db_missing_before_run or scheduled_full)

    logging.info("App: %s", APP_NAME)
    logging.info("Config: %s", config_path)
    logging.info("DB: %s", db_path)
    logging.info("Roots: %s", ", ".join(str(r) for r in roots))
    logging.info("Mode: %s", "FULL" if full_scan else f"RECENT-{cfg['recent_months']}-MONTHS")
    logging.info("Dry run: %s", dry_run)

    stats = Stats()
    db = DedupeDB(db_path)
    try:
        if full_scan and cfg.get("prune_missing_on_full_scan", True):
            missing = db.mark_missing_under_roots(roots)
            if missing:
                logging.info("Marked missing index entries: %d", missing)

        records = list(iter_files(roots, cfg, full_scan=full_scan, stats=stats))
        records.sort(key=lambda r: (r.size, str(r.path).lower()))
        logging.info("Eligible records collected: %d", len(records))
        process_records(records, db, cfg, stats, dry_run=dry_run)
    finally:
        db.close()

    logging.info("Done.")
    logging.info("Scanned files: %d", stats.scanned_files)
    logging.info("Eligible files: %d", stats.eligible_files)
    logging.info("Loaded unchanged hashes from DB: %d", stats.indexed_from_db)
    logging.info("New/changed files hashed: %d", stats.hashed_files)
    logging.info("Hardlinked duplicates: %d", stats.hardlinked_files)
    logging.info("Skipped recent: %d", stats.skipped_recent)
    logging.info("Skipped small: %d", stats.skipped_small)
    logging.info("Skipped locked: %d", stats.skipped_locked)
    logging.info("Skipped errors: %d", stats.skipped_errors)
    logging.info("Estimated saved space: %.2f MB", stats.saved_bytes / 1024 / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
