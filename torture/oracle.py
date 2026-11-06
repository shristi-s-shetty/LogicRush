"""The referee's notebook.

The database does not get to mark its own score. This module remembers which
operations printed ACK, then decides whether a dump is a legal outcome after a kill.

Legal outcomes:
- every ACKed operation is in the dump
- an operation that never printed ACK may be fully present or fully absent
- the server handles one operation at a time, so at most the first unfinished
  operation may have landed without an ACK
- a batch is one operation: all of its edits, or none of them
"""

from __future__ import annotations

import copy


class OracleError(Exception):
    """The dump cannot be explained by the ACK log."""


def apply_op(state: dict, op: dict) -> None:
    kind = op["kind"]
    if kind == "put":
        state[op["key"]] = op["value"]
    elif kind == "del":
        state.pop(op["key"], None)
    elif kind == "batch":
        for step in op["steps"]:
            apply_op(state, step)
    elif kind == "compact":
        return
    else:
        raise OracleError(f"unknown op kind {kind}")


def judge(baseline: dict, submitted: list, results: dict, dump: dict) -> dict:
    """Return which legal story matches the dump.

    results maps an op id to "ACK" or "ERR".
    A missing id means the kill landed before we saw a reply.
    """
    state = copy.deepcopy(baseline)
    for index, op in enumerate(submitted):
        status = results.get(op["id"])
        if status == "ACK":
            apply_op(state, op)
            continue
        if status == "ERR":
            continue
        for later in submitted[index + 1 :]:
            if later["id"] in results:
                raise OracleError(
                    f"op {later['id']} got {results[later['id']]} "
                    f"before earlier op {op['id']} finished"
                )
        without = state
        with_it = copy.deepcopy(state)
        apply_op(with_it, op)
        if dump == without:
            return {"match": "without-inflight", "inflight": op, "state": without}
        if dump == with_it:
            return {"match": "with-inflight", "inflight": op, "state": with_it}
        raise OracleError(_explain(op, without, with_it, dump))
    if dump != state:
        raise OracleError(_explain(None, state, None, dump))
    return {"match": "exact", "inflight": None, "state": state}


def _explain(inflight, without, with_it, dump) -> str:
    lines = ["dump does not match a legal state"]
    if inflight is not None:
        lines.append(f"in-flight op id={inflight['id']} kind={inflight['kind']}")
    lines.append("expected if the in-flight op is absent:")
    lines.extend(_preview(without))
    if with_it is not None:
        lines.append("expected if the in-flight op is fully present:")
        lines.extend(_preview(with_it))
    lines.append("dump:")
    lines.extend(_preview(dump))
    return "\n".join(lines)


def _preview(state: dict) -> list:
    if not state:
        return ["  (empty)"]
    lines = []
    for key in sorted(state):
        value = state[key]
        shown = value if len(value) <= 40 else value[:20] + f"...({len(value)} bytes)"
        lines.append(f"  {key}={shown}")
    return lines
