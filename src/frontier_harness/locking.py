"""Cross-platform advisory lock for one run directory.

The event ledger is transaction-safe, but two semantic controllers advancing the
same run would still duplicate expensive work.  A small OS-backed lock keeps one
active owner while preserving resumability after process exit.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from .errors import WorkspaceError


class _WindowsLockApi(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                windows_lock = cast(_WindowsLockApi, msvcrt)
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                windows_lock.locking(handle.fileno(), windows_lock.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise WorkspaceError(
                f"Run is already active in another process: {self.path.parent}"
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n".encode("ascii"))
        handle.flush()
        with suppress(OSError):
            os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                windows_lock = cast(_WindowsLockApi, msvcrt)
                handle.seek(0)
                windows_lock.locking(handle.fileno(), windows_lock.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
