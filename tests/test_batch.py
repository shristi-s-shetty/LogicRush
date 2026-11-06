import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kvdb


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "data.kv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_applies_in_order_and_survives_reopen(self):
        db = kvdb.Database()
        db.open(self.path)
        db.batch(
            [
                ("put", b"a", b"1"),
                ("put", b"b", b"2"),
                ("put", b"a", b"3"),
                ("del", b"b"),
            ]
        )
        self.assertEqual(db.get(b"a"), b"3")
        self.assertIsNone(db.get(b"b"))
        db.close()

        db = kvdb.Database()
        db.open(self.path)
        self.assertEqual(db.get(b"a"), b"3")
        self.assertIsNone(db.get(b"b"))
        db.close()

    def test_put_then_delete_same_key_in_one_batch(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"x", b"old")
        db.batch([("put", b"x", b"new"), ("del", b"x")])
        self.assertIsNone(db.get(b"x"))
        db.batch([("del", b"x"), ("put", b"x", b"back")])
        self.assertEqual(db.get(b"x"), b"back")
        db.close()

    def test_bad_batch_writes_nothing(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"keep", b"yes")
        with self.assertRaises(kvdb.KvError):
            db.batch([("put", b"ok", b"1"), ("put", b"", b"bad")])
        self.assertEqual(db.get(b"keep"), b"yes")
        self.assertIsNone(db.get(b"ok"))
        db.close()

    def test_too_many_operations(self):
        db = kvdb.Database()
        db.open(self.path)
        ops = [("put", b"k", b"v")] * (kvdb.MAX_BATCH + 1)
        with self.assertRaises(kvdb.KvError):
            db.batch(ops)
        db.close()


if __name__ == "__main__":
    unittest.main()
