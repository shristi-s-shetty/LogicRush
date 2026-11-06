import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kvdb


class DiskFullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "data.kv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_that_does_not_fit_leaves_old_data(self):
        db = kvdb.Database()
        db.open(self.path, max_bytes=200)
        db.put(b"a", b"1")
        with self.assertRaises(kvdb.DiskFullError):
            db.put(b"b", b"y" * 400)
        self.assertEqual(db.get(b"a"), b"1")
        self.assertIsNone(db.get(b"b"))
        db.close()

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"a"), b"1")
        self.assertIsNone(db.get(b"b"))
        db.close()

    def test_batch_that_does_not_fit_is_fully_absent(self):
        db = kvdb.Database()
        db.open(self.path, max_bytes=180)
        db.put(b"keep", b"yes")
        with self.assertRaises(kvdb.DiskFullError):
            db.batch(
                [
                    ("put", b"n1", b"abcdefghij"),
                    ("put", b"n2", b"abcdefghij"),
                    ("del", b"keep"),
                ]
            )
        self.assertEqual(db.get(b"keep"), b"yes")
        self.assertIsNone(db.get(b"n1"))
        self.assertIsNone(db.get(b"n2"))
        db.close()

    def test_deletes_and_compaction_allow_writes_again(self):
        db = kvdb.Database()
        db.open(self.path, max_bytes=5000)
        db.put(b"big", b"q" * 800)
        with self.assertRaises(kvdb.DiskFullError):
            db.put(b"more", b"z" * 4000)
        db.delete(b"big")
        db.compact()
        db.put(b"more", b"ok")
        self.assertEqual(db.get(b"more"), b"ok")
        self.assertIsNone(db.get(b"big"))
        self.assertLess(os.path.getsize(self.path), 5000)
        db.close()


if __name__ == "__main__":
    unittest.main()
