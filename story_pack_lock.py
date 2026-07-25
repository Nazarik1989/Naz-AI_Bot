"""Crash-safe cross-process lock shared by Story controls and renderer."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO


class StoryPackLockError(RuntimeError):
    pass


def ensure_private_group_access(path: Path, *, directory: bool) -> None:
    """Share a private Story path with the parent directory's Unix group.

    Naz main may run as root while the renderer runs as the unprivileged
    ``naz`` user.  Group inheritance keeps the queue private from everyone
    else while allowing both processes to update the same manifest and lock.
    Windows has no equivalent deployment ownership model, so this is a no-op
    there.
    """
    if os.name == "nt":
        return
    target = Path(path)
    if target.is_symlink():
        raise OSError("unsafe Story pack symlink")
    parent_gid = target.parent.stat().st_gid
    current = target.stat()
    if current.st_gid != parent_gid:
        os.chown(target, -1, parent_gid, follow_symlinks=False)
    desired_mode = 0o2770 if directory else 0o660
    if stat.S_IMODE(current.st_mode) != desired_mode:
        target.chmod(desired_mode)


class AdvisoryFileLock:
    """Non-blocking OS lock whose ownership is released on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.stream: BinaryIO | None = None
        self.backend = ""

    def __enter__(self) -> "AdvisoryFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            ensure_private_group_access(self.path, directory=False)
        except OSError:
            os.close(descriptor)
            raise
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
