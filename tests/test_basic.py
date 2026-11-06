import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kvdb


class BasicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "data.kv")

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, **kwargs):
        db = kvdb.Database()
        db.open(self.path, **kwargs)
        return db

    def test_put_get_delete_and_reopen(self):
        db = self.open()
        db.put(b"hello", b"world")
        self.assertEqual(db.get(b"hello"), b"world")
        db.delete(b"hello")
        self.assertIsNone(db.get(b"hello"))
        db.delete(b"hello")
        db.close()

        db = self.open()
        self.assertIsNone(db.get(b"hello"))
        db.put("cafe", "table")
        db.close()

        db = self.open()
        self.assertEqual(db.get(b"cafe"), b"table")
        db.close()

    def test_empty_value_and_overwrite(self):
        db = self.open()
        db.put(b"k", b"")
        self.assertEqual(db.get(b"k"), b"")
        db.put(b"k", b"second")
        self.assertEqual(db.get(b"k"), b"second")
        db.close()

    def test_missing_key(self):
        db = self.open()
        self.assertIsNone(db.get(b"nope"))
        db.close()

    def test_rejects_bad_sizes(self):
        db = self.open()
        with self.assertRaises(kvdb.KvError):
            db.put(b"", b"x")
        with self.assertRaises(kvdb.KvError):
            db.put(b"k" * 1025, b"x")
        with self.assertRaises(kvdb.KvError):
            db.put(b"k", b"v" * (kvdb.MAX_VALUE + 1))
        self.assertIsNone(db.get(b"k"))
        db.close()

    def test_escape_round_trip(self):
        raw = b"a\\b\n\t\xff"
        self.assertEqual(kvdb.unescape_bytes(kvdb.escape_bytes(raw)), raw)


if __name__ == "__main__":
    unittest.main()
