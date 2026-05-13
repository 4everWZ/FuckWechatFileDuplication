import tempfile
import unittest
from pathlib import Path

import fuck_wechat_file_duplication as dedupe


class DedupeSafetyTests(unittest.TestCase):
    def test_resolve_config_paths_expands_user_paths_and_skips_empty_values(self) -> None:
        paths = dedupe.resolve_config_paths(["~/Downloads", "", "D:\\Paper"])

        self.assertEqual(paths[0], Path("~/Downloads").expanduser())
        self.assertEqual(paths[1], Path("D:\\Paper"))

    def test_find_matching_source_file_uses_same_size_hash_and_content(self) -> None:
        content = b"A" * 70000

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "sources"
            target_root = root / "wechat"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "paper.pdf"
            wrong_same_size = source_root / "wrong.pdf"
            target = target_root / "paper.pdf"
            source.write_bytes(content)
            wrong_same_size.write_bytes(b"B" * len(content))
            target.write_bytes(content)

            target_record = dedupe.stat_record(target)
            self.assertIsNotNone(target_record)
            cfg = dict(dedupe.DEFAULT_CONFIG)
            matched = dedupe.find_matching_source_file(
                target_record,  # type: ignore[arg-type]
                [source_root],
                cfg,
                buffer_size=1024 * 1024,
            )

            self.assertIsNotNone(matched)
            self.assertEqual(matched[0], source)  # type: ignore[index]

    def test_link_to_matching_source_replaces_wechat_copy(self) -> None:
        content = b"A" * 70000

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "sources"
            target_root = root / "wechat"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "paper.pdf"
            target = target_root / "paper.pdf"
            source.write_bytes(content)
            target.write_bytes(content)

            target_record = dedupe.stat_record(target)
            self.assertIsNotNone(target_record)
            stats = dedupe.Stats()
            cfg = dict(dedupe.DEFAULT_CONFIG)

            linked = dedupe.link_to_matching_source(
                target_record,  # type: ignore[arg-type]
                [source_root],
                cfg,
                stats,
                dry_run=False,
            )

            self.assertTrue(linked)
            self.assertTrue(dedupe.is_same_physical_file(target, source))
            self.assertEqual(stats.source_hardlinked_files, 1)

    def test_link_to_matching_source_keeps_non_matching_same_size_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "sources"
            target_root = root / "wechat"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "paper.pdf"
            target = target_root / "paper.pdf"
            source.write_bytes(b"A" * 70000)
            target.write_bytes(b"B" * 70000)

            target_record = dedupe.stat_record(target)
            self.assertIsNotNone(target_record)
            stats = dedupe.Stats()
            cfg = dict(dedupe.DEFAULT_CONFIG)

            linked = dedupe.link_to_matching_source(
                target_record,  # type: ignore[arg-type]
                [source_root],
                cfg,
                stats,
                dry_run=False,
            )

            self.assertFalse(linked)
            self.assertFalse(dedupe.is_same_physical_file(target, source))
            self.assertEqual(target.read_bytes(), b"B" * 70000)
            self.assertEqual(stats.source_hardlinked_files, 0)

    def test_wait_for_stable_file_returns_record_after_quiet_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wechat-copy.bin"
            path.write_bytes(b"A" * 70000)

            record = dedupe.wait_for_stable_file(
                path,
                stable_seconds=0.02,
                poll_seconds=0.01,
                timeout_seconds=1.0,
            )

            self.assertIsNotNone(record)
            self.assertEqual(record.path, path)  # type: ignore[union-attr]
            self.assertEqual(record.size, 70000)  # type: ignore[union-attr]

    def test_make_backup_path_preserves_original_name_with_plain_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "report.pdf"
            backup = dedupe.make_backup_path(original)

            self.assertEqual(backup.parent, original.parent)
            self.assertFalse(backup.name.startswith("."))
            self.assertRegex(backup.name, r"^report\.pdf\.dedupe_backup\.\d+\.\d+$")
            self.assertEqual(dedupe.original_path_for_backup(backup), original)
            self.assertEqual(
                dedupe.original_path_for_backup(original.with_name(".report.pdf.dedupe_backup.123.456")),
                original,
            )

    def test_recover_interrupted_backup_restores_missing_original(self) -> None:
        content = b"A" * 70000

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "report.pdf"
            backup = root / "report.pdf.dedupe_backup.123.456"
            backup.write_bytes(content)

            stats = dedupe.recover_interrupted_backups([root], buffer_size=1024 * 1024)

            self.assertEqual(original.read_bytes(), content)
            self.assertFalse(backup.exists())
            self.assertEqual(stats.restored_backups, 1)
            self.assertEqual(stats.removed_backups, 0)
            self.assertEqual(stats.conflicted_backups, 0)

    def test_recover_interrupted_backup_removes_identical_backup(self) -> None:
        content = b"A" * 70000

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "report.pdf"
            backup = root / "report.pdf.dedupe_backup.123.456"
            original.write_bytes(content)
            backup.write_bytes(content)

            stats = dedupe.recover_interrupted_backups([root], buffer_size=1024 * 1024)

            self.assertEqual(original.read_bytes(), content)
            self.assertFalse(backup.exists())
            self.assertEqual(stats.restored_backups, 0)
            self.assertEqual(stats.removed_backups, 1)
            self.assertEqual(stats.conflicted_backups, 0)

    def test_recover_interrupted_backup_keeps_conflicting_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "report.pdf"
            backup = root / "report.pdf.dedupe_backup.123.456"
            original.write_bytes(b"A" * 70000)
            backup.write_bytes(b"B" * 70000)

            with self.assertLogs(level="WARNING") as logs:
                stats = dedupe.recover_interrupted_backups([root], buffer_size=1024 * 1024)

            self.assertEqual(original.read_bytes(), b"A" * 70000)
            self.assertEqual(backup.read_bytes(), b"B" * 70000)
            self.assertEqual(stats.restored_backups, 0)
            self.assertEqual(stats.removed_backups, 0)
            self.assertEqual(stats.conflicted_backups, 1)
            self.assertIn("Backup recovery conflict", logs.output[0])

    def test_iter_files_skips_dedupe_backup_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal = root / "normal.bin"
            backup = root / "normal.bin.dedupe_backup.123.456"
            normal.write_bytes(b"A" * 70000)
            backup.write_bytes(b"A" * 70000)

            cfg = dict(dedupe.DEFAULT_CONFIG)
            cfg["min_size_bytes"] = 1
            cfg["skip_recent_hours"] = -1
            stats = dedupe.Stats()

            records = list(dedupe.iter_files([root], cfg, full_scan=True, stats=stats))

            self.assertEqual([record.path for record in records], [normal])

    def test_matching_duplicate_is_hardlinked(self) -> None:
        content = b"A" * 70000

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical.bin"
            duplicate = root / "duplicate.bin"
            db_path = root / "index.sqlite3"

            canonical.write_bytes(content)
            duplicate.write_bytes(content)

            db = dedupe.DedupeDB(db_path)
            try:
                canonical_record = dedupe.stat_record(canonical)
                duplicate_record = dedupe.stat_record(duplicate)
                self.assertIsNotNone(canonical_record)
                self.assertIsNotNone(duplicate_record)
                digest = dedupe.hash_file(canonical, 1024 * 1024)
                db.upsert(canonical_record, digest)  # type: ignore[arg-type]
                db.commit()

                stats = dedupe.Stats()
                cfg = dict(dedupe.DEFAULT_CONFIG)

                dedupe.process_records(
                    [duplicate_record],  # type: ignore[list-item]
                    db,
                    cfg,
                    stats,
                    dry_run=False,
                )

                self.assertTrue(dedupe.is_same_physical_file(duplicate, canonical))
                self.assertEqual(duplicate.read_bytes(), content)
                self.assertEqual(stats.hardlinked_files, 1)
            finally:
                db.close()

    def test_stale_index_candidate_is_not_used_for_hardlink(self) -> None:
        old_content = b"A" * 70000
        changed_content = b"B" * 70000

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical.bin"
            duplicate = root / "duplicate.bin"
            db_path = root / "index.sqlite3"

            canonical.write_bytes(old_content)
            duplicate.write_bytes(old_content)

            db = dedupe.DedupeDB(db_path)
            try:
                canonical_record = dedupe.stat_record(canonical)
                self.assertIsNotNone(canonical_record)
                old_digest = dedupe.hash_file(canonical, 1024 * 1024)
                db.upsert(canonical_record, old_digest)  # type: ignore[arg-type]
                db.commit()

                canonical.write_bytes(changed_content)

                duplicate_record = dedupe.stat_record(duplicate)
                self.assertIsNotNone(duplicate_record)
                stats = dedupe.Stats()
                cfg = dict(dedupe.DEFAULT_CONFIG)

                dedupe.process_records(
                    [duplicate_record],  # type: ignore[list-item]
                    db,
                    cfg,
                    stats,
                    dry_run=False,
                )

                self.assertEqual(duplicate.read_bytes(), old_content)
                self.assertFalse(dedupe.is_same_physical_file(duplicate, canonical))
                self.assertEqual(stats.hardlinked_files, 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
