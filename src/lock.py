"""Cross-platform advisory file locking with crash reclamation."""

from __future__ import annotations

import os

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


class LockError(Exception):
    """Raised when the lock cannot be acquired."""


class FileLock:
    def __init__(self, path: str) -> None:
        self.lock_path = path + ".lock"
        self._handle = None

    def acquire(self) -> None:
        handle = open(self.lock_path, "a+b")
        handle.seek(0)
        try:
            if msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockError("database is locked by another process") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.seek(0)
                if msvcrt is not None:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None

