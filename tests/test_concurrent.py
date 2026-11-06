import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kvdb


class ConcurrentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "data.kv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_writers_do_not_mix_values(self):
        db = kvdb.Database()
        db.open(self.path)
        errors = []

        def writer(prefix):
            try:
                for i in range(40):
                    db.put(f"{prefix}-{i}".encode(), prefix.encode() * 20)
                    db.put(b"shared", prefix.encode())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=("A",)), threading.Thread(target=writer, args=("B",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for prefix in (b"A", b"B"):
            for i in range(40):
                self.assertEqual(db.get(prefix + b"-" + str(i).encode()), prefix * 20)
        shared = db.get(b"shared")
        self.assertIn(shared, (b"A", b"B"))
        db.close()

    def test_reader_during_batch_sees_before_or_after(self):
        db = kvdb.Database()
        db.open(self.path)
        db.put(b"k", b"before")
        seen = []

        def reader():
            for _ in range(30):
                value = db.get(b"k")
                seen.append(value)

        def writer():
            db.batch([("put", b"k", b"after"), ("put", b"other", b"1")])

        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(seen)
        self.assertTrue(all(value in (b"before", b"after") for value in seen))
        self.assertEqual(db.get(b"k"), b"after")
        db.close()


if __name__ == "__main__":
    unittest.main()
