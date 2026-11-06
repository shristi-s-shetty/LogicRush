import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "torture"))

from oracle import OracleError, judge


def put(op_id, key, value):
    return {"id": op_id, "kind": "put", "key": key, "value": value}


class OracleTests(unittest.TestCase):
    def test_acked_put_must_be_present(self):
        op = put("1", "a", "one")
        found = judge({}, [op], {"1": "ACK"}, {"a": "one"})
        self.assertEqual(found["match"], "exact")

    def test_missing_ack_may_be_present_or_absent(self):
        op = put("1", "a", "one")
        absent = judge({}, [op], {}, {})
        present = judge({}, [op], {}, {"a": "one"})
        self.assertEqual(absent["match"], "without-inflight")
        self.assertEqual(present["match"], "with-inflight")

    def test_half_a_batch_is_illegal(self):
        op = {
            "id": "9",
            "kind": "batch",
            "steps": [
                {"kind": "put", "key": "a", "value": "1"},
                {"kind": "put", "key": "b", "value": "2"},
            ],
        }
        with self.assertRaises(OracleError):
            judge({}, [op], {}, {"a": "1"})

    def test_unknown_value_is_illegal(self):
        op = put("1", "a", "one")
        with self.assertRaises(OracleError):
            judge({}, [op], {"1": "ACK"}, {"a": "other"})

    def test_later_unacked_op_cannot_explain_the_dump(self):
        first = put("1", "a", "one")
        second = put("2", "b", "two")
        with self.assertRaises(OracleError):
            judge({}, [first, second], {}, {"b": "two"})

    def test_error_reply_does_not_change_state(self):
        op = put("1", "a", "one")
        found = judge({}, [op], {"1": "ERR"}, {})
        self.assertEqual(found["match"], "exact")
        self.assertEqual(found["state"], {})

    def test_baseline_survives_a_later_crash(self):
        baseline = {"a": "kept"}
        op = put("2", "b", "new")
        found = judge(copy.deepcopy(baseline), [op], {}, dict(baseline))
        self.assertEqual(found["state"]["a"], "kept")


if __name__ == "__main__":
    unittest.main()
