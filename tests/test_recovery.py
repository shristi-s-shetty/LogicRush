import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kvdb


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "data.kv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_torn_tail_is_ignored_and_second_open_is_stable(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"kept", b"yes")
        db.close()

        torn = kvdb.pack_frame(
            kvdb.KIND_PUT, 99, kvdb.pack_put_payload(b"ghost", b"no")
        )
        with open(self.path, "ab") as handle:
            handle.write(torn)
            handle.write(b"\x00\x01\x02")

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"kept"), b"yes")
        self.assertIsNone(db.get(b"ghost"))
        self.assertGreater(db.recovered_torn_bytes, 0)
        db.close()

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.recovered_torn_bytes, 0)
        self.assertEqual(db.get(b"kept"), b"yes")
        self.assertEqual(db.check(), "OK")
        db.close()

    def test_frames_without_commit_marker_stay_invisible(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"old", b"1")
        db.close()

        frame = kvdb.pack_frame(
            kvdb.KIND_PUT, 50, kvdb.pack_put_payload(b"new", b"2")
        )
        with open(self.path, "ab") as handle:
            handle.write(frame)

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"old"), b"1")
        self.assertIsNone(db.get(b"new"))
        db.close()

    def test_complete_commit_marker_is_visible_even_without_a_return(self):
        """Scenario A4, simulated: the commit record is on disk, then we stop."""
        db = kvdb.Database()
        db.open(self.path)
        db.close()

        txn = 1
        put = kvdb.pack_frame(kvdb.KIND_PUT, txn, kvdb.pack_put_payload(b"k", b"v"))
        commit = kvdb.pack_frame(kvdb.KIND_COMMIT, txn, (1).to_bytes(4, "little"))
        with open(self.path, "ab") as handle:
            handle.write(put)
            handle.flush()
            os.fsync(handle.fileno())
            handle.write(commit)
            handle.flush()
            os.fsync(handle.fileno())

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"k"), b"v")
        db.close()

    def test_one_byte_chop_loses_only_the_unfinished_tail(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"a", b"1")
        db.put(b"b", b"2")
        db.close()
        size = os.path.getsize(self.path)
        with open(self.path, "r+b") as handle:
            handle.truncate(size - 1)

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"a"), b"1")
        self.assertIsNone(db.get(b"b"))
        db.close()

    def test_compaction_keeps_live_keys_and_shrinks(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"a", b"x" * 2000)
        db.put(b"a", b"short")
        db.put(b"gone", b"z" * 2000)
        db.delete(b"gone")
        before = os.path.getsize(self.path)
        db.compact()
        after = os.path.getsize(self.path)
        self.assertLess(after, before)
        self.assertEqual(db.get(b"a"), b"short")
        self.assertIsNone(db.get(b"gone"))
        db.close()

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"a"), b"short")
        self.assertEqual(db.check(), "OK")
        db.close()

    def test_leftover_compaction_file_is_removed_and_old_data_remains(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"a", b"1")
        db.close()
        with open(self.path + ".compacting", "wb") as handle:
            handle.write(b"not a database")

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"a"), b"1")
        self.assertFalse(os.path.exists(self.path + ".compacting"))
        db.close()


if __name__ == "__main__":
    unittest.main()
