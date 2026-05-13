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
import queue
import re
import sqlite3
import stat as stat_module
import subprocess
import sys
import threading
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
BACKUP_SUFFIX_RE = re.compile(r"^(?P<original>.+)\.dedupe_backup\.(?P<pid>\d+)\.(?P<timestamp>\d+)$")
LEGACY_BACKUP_SUFFIX_RE = re.compile(r"^\.(?P<original>.+)\.dedupe_backup\.(?P<pid>\d+)\.(?P<timestamp>\d+)$")
TEMP_LINK_SUFFIX_RE = re.compile(r"^(?P<original>.+)\.dedupe_link\.(?P<pid>\d+)\.(?P<timestamp>\d+)$")
DEFAULT_CONFIG = {
    "roots": [r"D:\\xwechat_files"],
    "source_roots": [r"~\\Downloads", r"~\\Desktop", r"D:\\Paper"],
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
    "byte_compare_before_link": True,
    "same_volume_only": True,
    "hash_buffer_mb": 8,
    "watch_stable_seconds": 8,
    "watch_poll_seconds": 1,
    "watch_timeout_seconds": 120,
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
    source_candidates_scanned: int = 0
    source_candidates_hashed: int = 0
    source_hardlinked_files: int = 0
    saved_bytes: int = 0


@dataclass
class BackupRecoveryStats:
    restored_backups: int = 0
    removed_backups: int = 0
    conflicted_backups: int = 0
    errors: int = 0


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


def resolve_config_paths(paths: Iterable[str]) -> List[Path]:
    resolved = []
    for raw in paths:
        value = str(raw).strip()
        if not value:
            continue
        resolved.append(Path(value).expanduser())
    return resolved


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


def make_backup_path(path: Path) -> Path:
    timestamp_ms = int(time.time() * 1000)
    for offset in range(1000):
        candidate = path.with_name(
            f"{path.name}.dedupe_backup.{os.getpid()}.{timestamp_ms + offset}"
        )
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name}.dedupe_backup.{os.getpid()}.{time.time_ns()}")


def make_temp_link_path(path: Path) -> Path:
    timestamp_ms = int(time.time() * 1000)
    for offset in range(1000):
        candidate = path.with_name(
            f"{path.name}.dedupe_link.{os.getpid()}.{timestamp_ms + offset}"
        )
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name}.dedupe_link.{os.getpid()}.{time.time_ns()}")


def original_path_for_backup(path: Path) -> Optional[Path]:
    legacy_match = LEGACY_BACKUP_SUFFIX_RE.match(path.name)
    if legacy_match is not None:
        return path.with_name(legacy_match.group("original"))
    match = BACKUP_SUFFIX_RE.match(path.name)
    if match is None:
        return None
    return path.with_name(match.group("original"))


def is_dedupe_backup_path(path: Path) -> bool:
    return original_path_for_backup(path) is not None


def is_dedupe_temp_link_path(path: Path) -> bool:
    return TEMP_LINK_SUFFIX_RE.match(path.name) is not None


def is_dedupe_internal_path(path: Path) -> bool:
    return is_dedupe_backup_path(path) or is_dedupe_temp_link_path(path)


def make_path_writable(path: Path) -> None:
    try:
        current_mode = path.stat().st_mode
        os.chmod(path, current_mode | stat_module.S_IWRITE)
    except OSError:
        # Let the following rename/unlink report the concrete operation failure.
        return


def delete_path_with_retries(path: Path, attempts: int = 3, delay_seconds: float = 0.2) -> bool:
    for attempt in range(max(1, attempts)):
        if not path.exists():
            return True
        make_path_writable(path)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt == max(1, attempts) - 1:
                logging.debug("Delete failed after retries: %s | %s", path, exc)
                return False
            time.sleep(max(delay_seconds, 0.0))
    return not path.exists()


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
                    if is_dedupe_internal_path(p):
                        continue
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


def records_still_match(expected: FileRecord, actual: FileRecord) -> bool:
    if actual.size != expected.size or actual.mtime_ns != expected.mtime_ns:
        return False
    if expected.st_dev and actual.st_dev and actual.st_dev != expected.st_dev:
        return False
    if expected.st_ino and actual.st_ino and actual.st_ino != expected.st_ino:
        return False
    return True


def files_have_same_content(left: Path, right: Path, buffer_size: int) -> bool:
    with left.open("rb", buffering=0) as left_file, right.open("rb", buffering=0) as right_file:
        while True:
            left_chunk = left_file.read(buffer_size)
            right_chunk = right_file.read(buffer_size)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def wait_for_stable_file(
    path: Path,
    stable_seconds: float,
    poll_seconds: float,
    timeout_seconds: float,
) -> Optional[FileRecord]:
    deadline = time.time() + max(timeout_seconds, poll_seconds)
    stable_since: Optional[float] = None
    last_signature: Optional[Tuple[int, int]] = None
    poll = max(poll_seconds, 0.01)
    required_stable = max(stable_seconds, 0.0)

    while time.time() <= deadline:
        record = stat_record(path)
        if record is None or path.is_symlink():
            stable_since = None
            last_signature = None
            time.sleep(poll)
            continue

        signature = (record.size, record.mtime_ns)
        now = time.time()
        if signature != last_signature:
            last_signature = signature
            stable_since = now
        elif stable_since is not None and now - stable_since >= required_stable:
            return record
        time.sleep(poll)
    return None


def iter_source_candidates_by_size(
    source_roots: Sequence[Path],
    target: FileRecord,
    cfg: Dict,
    stats: Optional[Stats] = None,
) -> Iterator[FileRecord]:
    exclude_dirs = {str(x).lower() for x in cfg.get("exclude_dir_names", [])}
    exclude_exts = normalize_extensions(cfg.get("exclude_file_extensions", []))

    for root in source_roots:
        if not root.exists():
            logging.debug("Source root does not exist, skipped: %s", root)
            continue
        if cfg.get("same_volume_only", True) and not is_same_volume(target.path, root):
            logging.debug("Source root is on a different volume, skipped: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [dirname for dirname in dirnames if dirname.lower() not in exclude_dirs]
            current = Path(dirpath)
            for filename in filenames:
                candidate = current / filename
                try:
                    if is_dedupe_internal_path(candidate):
                        continue
                    if exclude_exts and candidate.suffix.lower() in exclude_exts:
                        continue
                    if candidate.is_symlink():
                        continue
                    if str(candidate) == str(target.path):
                        continue
                    if cfg.get("same_volume_only", True) and not is_same_volume(target.path, candidate):
                        continue
                    if is_same_physical_file(target.path, candidate):
                        continue
                    record = stat_record(candidate)
                    if record is None:
                        continue
                    if stats is not None:
                        stats.source_candidates_scanned += 1
                    if record.size != target.size:
                        continue
                    yield record
                except OSError as exc:
                    if stats is not None:
                        stats.skipped_errors += 1
                    logging.debug("Source candidate stat failed: %s | %s", candidate, exc)


def find_matching_source_file(
    target: FileRecord,
    source_roots: Sequence[Path],
    cfg: Dict,
    buffer_size: int,
    stats: Optional[Stats] = None,
) -> Optional[Tuple[Path, FileRecord]]:
    target_before = stat_record(target.path)
    if target_before is None or not records_still_match(target, target_before):
        return None

    try:
        target_digest = hash_file(target.path, buffer_size)
    except OSError as exc:
        logging.debug("Target hash failed before source lookup: %s | %s", target.path, exc)
        return None

    target_after = stat_record(target.path)
    if target_after is None or not records_still_match(target_before, target_after):
        logging.debug("Target changed during source lookup hash, skipped: %s", target.path)
        return None

    for candidate in iter_source_candidates_by_size(source_roots, target_after, cfg, stats):
        candidate_before = stat_record(candidate.path)
        if candidate_before is None or not records_still_match(candidate, candidate_before):
            continue
        try:
            candidate_digest = hash_file(candidate.path, buffer_size)
        except OSError as exc:
            logging.debug("Source hash failed: %s | %s", candidate.path, exc)
            continue
        if stats is not None:
            stats.source_candidates_hashed += 1
        candidate_after = stat_record(candidate.path)
        if candidate_after is None or not records_still_match(candidate_before, candidate_after):
            logging.debug("Source changed during hash, skipped: %s", candidate.path)
            continue
        if candidate_digest != target_digest:
            continue
        try:
            if not files_have_same_content(target.path, candidate.path, buffer_size):
                continue
        except OSError as exc:
            logging.debug("Source byte compare failed: %s <-> %s | %s", target.path, candidate.path, exc)
            continue
        target_latest = stat_record(target.path)
        candidate_latest = stat_record(candidate.path)
        if target_latest is None or candidate_latest is None:
            continue
        if not records_still_match(target_after, target_latest):
            logging.debug("Target changed before source match finalized: %s", target.path)
            return None
        if not records_still_match(candidate_after, candidate_latest):
            logging.debug("Source changed before source match finalized: %s", candidate.path)
            continue
        return candidate.path, candidate_latest
    return None


def link_to_matching_source(
    target: FileRecord,
    source_roots: Sequence[Path],
    cfg: Dict,
    stats: Stats,
    dry_run: bool,
) -> bool:
    buffer_size = max(1, int(cfg.get("hash_buffer_mb", 8))) * 1024 * 1024
    matched = find_matching_source_file(target, source_roots, cfg, buffer_size, stats)
    if matched is None:
        return False
    source_path, source_record = matched
    ok, _ = hardlink_duplicate(
        target,
        source_path,
        cfg,
        dry_run=dry_run,
        canonical_record=source_record,
    )
    if ok:
        stats.source_hardlinked_files += 1
        stats.hardlinked_files += 1
        stats.saved_bytes += target.size
    return ok


def handle_watch_path(
    path: Path,
    source_roots: Sequence[Path],
    cfg: Dict,
    stats: Stats,
    dry_run: bool,
) -> bool:
    if is_dedupe_internal_path(path) or path.is_symlink():
        return False

    stable = wait_for_stable_file(
        path,
        stable_seconds=float(cfg.get("watch_stable_seconds", 8)),
        poll_seconds=float(cfg.get("watch_poll_seconds", 1)),
        timeout_seconds=float(cfg.get("watch_timeout_seconds", 120)),
    )
    if stable is None:
        logging.debug("Watch target did not stabilize before timeout: %s", path)
        return False
    if stable.size < int(cfg.get("min_size_bytes", 0)):
        return False
    logging.info("Watch processing stable file: %s", path)
    linked = link_to_matching_source(stable, source_roots, cfg, stats, dry_run=dry_run)
    if not linked:
        logging.debug("Watch found no matching source for: %s", path)
    return linked


def run_watch(roots: Sequence[Path], source_roots: Sequence[Path], cfg: Dict, dry_run: bool) -> int:
    try:
        from watchdog.events import FileSystemEventHandler  # type: ignore
        from watchdog.observers import Observer  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: watchdog\n"
            "Install it with:\n"
            "  python -m pip install -r requirements.txt\n"
        ) from exc

    existing_roots = [root for root in roots if root.exists()]
    existing_source_roots = [root for root in source_roots if root.exists()]
    if not existing_roots:
        logging.error("No existing WeChat roots to watch.")
        return 1
    if not existing_source_roots:
        logging.error("No existing source_roots to match against.")
        return 1

    stats = Stats()
    work_queue: "queue.Queue[Path]" = queue.Queue()
    stop_event = threading.Event()

    def enqueue(path_str: str) -> None:
        path = Path(path_str)
        if is_dedupe_internal_path(path):
            return
        work_queue.put(path)

    class WeChatFileEventHandler(FileSystemEventHandler):  # type: ignore[misc]
        def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory:
                enqueue(event.src_path)

        def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory:
                enqueue(event.src_path)

        def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory:
                enqueue(event.dest_path)

    def worker() -> None:
        last_attempts: Dict[Path, float] = {}
        while not stop_event.is_set():
            try:
                path = work_queue.get(timeout=1)
            except queue.Empty:
                continue
            now = time.time()
            last_attempt = last_attempts.get(path)
            if last_attempt is not None and now - last_attempt < float(cfg.get("watch_poll_seconds", 1)):
                work_queue.task_done()
                continue
            last_attempts[path] = now
            try:
                if handle_watch_path(path, existing_source_roots, cfg, stats, dry_run):
                    logging.info(
                        "Watch linked=%d | source_candidates=%d | source_hashed=%d | saved=%.2f MB",
                        stats.source_hardlinked_files,
                        stats.source_candidates_scanned,
                        stats.source_candidates_hashed,
                        stats.saved_bytes / 1024 / 1024,
                    )
            except Exception:
                logging.exception("Watch worker failed while processing: %s", path)
            finally:
                work_queue.task_done()

    observer = Observer()
    handler = WeChatFileEventHandler()
    for root in existing_roots:
        observer.schedule(handler, str(root), recursive=True)
        logging.info("Watching WeChat root: %s", root)
    logging.info("Source roots: %s", ", ".join(str(root) for root in existing_source_roots))

    worker_thread = threading.Thread(target=worker, name="wechat-dedupe-watch-worker", daemon=True)
    worker_thread.start()
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Watch mode interrupted.")
    finally:
        stop_event.set()
        observer.stop()
        observer.join()
    return 0


def recover_interrupted_backups(roots: Sequence[Path], buffer_size: int) -> BackupRecoveryStats:
    stats = BackupRecoveryStats()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, _, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                backup = Path(dirpath) / filename
                if is_dedupe_temp_link_path(backup):
                    try:
                        if backup.is_symlink() or not backup.is_file():
                            continue
                        if delete_path_with_retries(backup):
                            stats.removed_backups += 1
                            logging.info("Removed interrupted temp hardlink: %s", backup)
                        else:
                            stats.errors += 1
                            logging.warning("Temp hardlink cleanup failed, left in place: %s", backup)
                    except OSError as exc:
                        stats.errors += 1
                        logging.warning("Temp hardlink cleanup failed, left in place: %s | %s", backup, exc)
                    continue
                original = original_path_for_backup(backup)
                if original is None:
                    continue
                try:
                    if backup.is_symlink() or not backup.is_file():
                        continue
                    if not original.exists():
                        make_path_writable(backup)
                        backup.rename(original)
                        stats.restored_backups += 1
                        logging.info("Recovered interrupted backup: %s -> %s", backup, original)
                        continue
                    if is_same_physical_file(backup, original):
                        if delete_path_with_retries(backup):
                            stats.removed_backups += 1
                            logging.info("Removed redundant backup hardlink: %s", backup)
                        else:
                            stats.errors += 1
                            logging.warning("Redundant backup cleanup failed, left in place: %s", backup)
                        continue

                    backup_rec = stat_record(backup)
                    original_rec = stat_record(original)
                    if backup_rec is None or original_rec is None:
                        stats.errors += 1
                        logging.warning("Backup recovery stat failed, left in place: %s", backup)
                        continue
                    if backup_rec.size == original_rec.size and files_have_same_content(
                        backup,
                        original,
                        buffer_size,
                    ):
                        if delete_path_with_retries(backup):
                            stats.removed_backups += 1
                            logging.info("Removed redundant backup with identical content: %s", backup)
                        else:
                            stats.errors += 1
                            logging.warning("Identical backup cleanup failed, left in place: %s", backup)
                        continue

                    stats.conflicted_backups += 1
                    logging.warning(
                        "Backup recovery conflict, left in place: backup=%s original=%s",
                        backup,
                        original,
                    )
                except OSError as exc:
                    stats.errors += 1
                    logging.warning("Backup recovery failed, left in place: %s | %s", backup, exc)
    return stats


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


def verify_candidate_content(
    current: FileRecord,
    candidate: Path,
    buffer_size: int,
) -> Optional[FileRecord]:
    current_before = stat_record(current.path)
    candidate_before = stat_record(candidate)
    if current_before is None or candidate_before is None:
        return None
    if not records_still_match(current, current_before):
        logging.debug("Current file changed before candidate verification: %s", current.path)
        return None
    if candidate_before.size != current.size:
        return None

    try:
        same_content = files_have_same_content(current.path, candidate, buffer_size)
    except OSError as exc:
        logging.debug("Content compare failed: %s <-> %s | %s", current.path, candidate, exc)
        return None

    current_after = stat_record(current.path)
    candidate_after = stat_record(candidate)
    if current_after is None or candidate_after is None:
        return None
    if not records_still_match(current_before, current_after):
        logging.debug("Current file changed during candidate verification: %s", current.path)
        return None
    if not records_still_match(candidate_before, candidate_after):
        logging.debug("Candidate changed during verification, skipped: %s", candidate)
        return None
    if not same_content:
        logging.debug("Candidate content does not match current file, skipped: %s", candidate)
        return None
    return candidate_after


def select_canonical(
    current: FileRecord,
    candidates: Sequence[Path],
    cfg: Dict,
    buffer_size: int,
) -> Optional[Tuple[Path, Optional[FileRecord]]]:
    byte_compare = bool(cfg.get("byte_compare_before_link", True))
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
        if byte_compare:
            cand_rec = verify_candidate_content(current, candidate, buffer_size)
            if cand_rec is None:
                continue
        return candidate, cand_rec
    return None


def hardlink_duplicate(
    duplicate: FileRecord,
    canonical: Path,
    cfg: Dict,
    dry_run: bool,
    canonical_record: Optional[FileRecord] = None,
) -> Tuple[bool, Optional[FileRecord]]:
    if cfg.get("verify_before_link", True):
        latest = stat_record(duplicate.path)
        if latest is None:
            logging.debug("Duplicate disappeared before link: %s", duplicate.path)
            return False, None
        if latest.size != duplicate.size or latest.mtime_ns != duplicate.mtime_ns:
            logging.debug("Duplicate changed before link, skipped: %s", duplicate.path)
            return False, latest
        if canonical_record is not None:
            latest_canonical = stat_record(canonical)
            if latest_canonical is None:
                logging.debug("Canonical disappeared before link: %s", canonical)
                return False, latest
            if not records_still_match(canonical_record, latest_canonical):
                logging.debug("Canonical changed before link, skipped: %s", canonical)
                return False, latest

    if dry_run:
        logging.info("[DRY-RUN] duplicate -> hardlink: %s -> %s", duplicate.path, canonical)
        return True, duplicate

    tmp_link = make_temp_link_path(duplicate.path)
    original_mode: Optional[int] = None

    try:
        os.link(str(canonical), str(tmp_link))
        try:
            original_mode = duplicate.path.stat().st_mode
        except OSError:
            original_mode = None
        make_path_writable(duplicate.path)
        try:
            os.replace(str(tmp_link), str(duplicate.path))
        except OSError:
            if (
                original_mode is not None
                and duplicate.path.exists()
                and not is_same_physical_file(duplicate.path, canonical)
            ):
                try:
                    os.chmod(duplicate.path, original_mode)
                except OSError:
                    pass
            if not delete_path_with_retries(tmp_link):
                logging.warning("Hardlink temp cleanup failed, left in place: %s", tmp_link)
            raise

        linked = stat_record(duplicate.path)
        logging.info("hardlinked: %s -> %s", duplicate.path, canonical)
        return True, linked
    except OSError as exc:
        if tmp_link.exists() and not delete_path_with_retries(tmp_link):
            logging.warning("Hardlink temp cleanup failed, left in place: %s", tmp_link)
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
        selected = select_canonical(rec, candidates, cfg, buffer_size)
        if selected is None:
            db.upsert(rec, digest)
            continue
        canonical, canonical_record = selected

        ok, linked_record = hardlink_duplicate(
            rec,
            canonical,
            cfg,
            dry_run=dry_run,
            canonical_record=canonical_record,
        )
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
    parser.add_argument("--watch", action="store_true", help="Watch WeChat roots and link new copies to source_roots.")
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
    roots = resolve_config_paths(cfg.get("roots", []))
    source_roots = resolve_config_paths(cfg.get("source_roots", []))

    db_path = Path(cfg.get("db_path", DEFAULT_CONFIG["db_path"]))
    if not db_path.is_absolute():
        db_path = script_dir / db_path

    log_dir = Path(cfg.get("log_dir", DEFAULT_CONFIG["log_dir"]))
    if not log_dir.is_absolute():
        log_dir = script_dir / log_dir
    setup_logging(log_dir)

    dry_run = bool(args.dry_run or cfg.get("dry_run", False))

    if args.kill_wechat or cfg.get("kill_wechat_before_run", False):
        kill_wechat_processes()
        time.sleep(2)

    recovery_buffer_size = max(1, int(cfg.get("hash_buffer_mb", 8))) * 1024 * 1024
    if dry_run:
        logging.info("Backup recovery skipped in dry-run mode.")
    elif args.watch:
        logging.info("Backup recovery skipped in watch mode; weekly task handles full recovery.")
    else:
        recovery = recover_interrupted_backups(roots, recovery_buffer_size)
        if (
            recovery.restored_backups
            or recovery.removed_backups
            or recovery.conflicted_backups
            or recovery.errors
        ):
            logging.info(
                "Backup recovery: restored=%d | removed=%d | conflicts=%d | errors=%d",
                recovery.restored_backups,
                recovery.removed_backups,
                recovery.conflicted_backups,
                recovery.errors,
            )

    db_missing_before_run = not db_path.exists()
    scheduled_full = should_do_scheduled_full_scan(cfg)
    full_scan = bool(args.full or db_missing_before_run or scheduled_full)

    logging.info("App: %s", APP_NAME)
    logging.info("Config: %s", config_path)
    logging.info("DB: %s", db_path)
    logging.info("Roots: %s", ", ".join(str(r) for r in roots))
    if source_roots:
        logging.info("Source roots: %s", ", ".join(str(r) for r in source_roots))
    logging.info("Mode: %s", "FULL" if full_scan else f"RECENT-{cfg['recent_months']}-MONTHS")
    logging.info("Dry run: %s", dry_run)

    if args.watch:
        logging.info("Watch mode: ON")
        return run_watch(roots, source_roots, cfg, dry_run=dry_run)

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
