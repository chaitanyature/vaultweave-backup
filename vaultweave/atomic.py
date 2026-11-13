"""POSIX publication primitives; fail if the platform cannot guarantee no replacement."""

import ctypes
import errno
import fcntl
import os
from pathlib import Path
import sys
from contextlib import contextmanager


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_new(source, target):
    # POSIX rename() may silently replace an empty destination directory.
    libc = ctypes.CDLL(None, use_errno=True)
    src, dst = os.fsencode(source), os.fsencode(target)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rc = libc.renameat2(-100, src, -100, dst, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rc = libc.renamex_np(src, dst, 4)  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, "exclusive directory rename unavailable")
    if rc:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


@contextmanager
def lock_repository(path, exclusive):
    fd = os.open(Path(path) / "LOCK", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def destination(path):
    path = Path(path)
    if not path.name or path.name in (".", ".."):
        raise ValueError("destination requires a new directory name")
    return path.parent.resolve(strict=True) / path.name
