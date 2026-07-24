"""Crash-safe cross-process lock shared by Story controls and renderer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class StoryPackLockError(RuntimeError):
    pass


class AdvisoryFileLock:
    """Non-blocking OS lock whose ownership is released on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.stream: BinaryIO | None = None
        self.backend = ""

    def __enter__(self) -> "AdvisoryFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    stream.write(b"0")
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                self.backend = "msvcrt"
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.backend = "fcntl"
            stream.seek(0)
            stream.write(str(os.getpid()).encode("ascii")[:20].ljust(20, b" "))
            stream.flush()
            self.stream = stream
            return self
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise StoryPackLockError("pack_locked") from exc

    def __exit__(self, *_: object) -> None:
        stream = self.stream
        self.stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if self.backend == "msvcrt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            elif self.backend == "fcntl":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class StoryPackLock(AdvisoryFileLock):
    def __init__(self, pack_dir: Path) -> None:
        super().__init__(Path(pack_dir) / ".pack.lock")
