import tempfile
import unittest
from pathlib import Path

import fuck_wechat_file_duplication as dedupe


class DedupeSafetyTests(unittest.TestCase):
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
