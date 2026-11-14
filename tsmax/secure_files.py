"""Small descriptor-relative file operations for map state and integration.

Every directory component and final file is opened without following symlinks.
These functions deliberately refuse platforms without the required primitives.
"""
from __future__ import annotations

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_BYTES = 64_000_000
_UNSET = object()


@contextmanager
def _parent(path: Path, create: bool = False):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("secure map file operations are unavailable on this platform")
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd, path.name
    finally:
        os.close(fd)


def _read_at(fd: int, name: str, limit: int) -> bytes | None:
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise OSError("map files must be bounded regular files without links")
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(child, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("file changed while opening")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if len(raw) > limit or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError("file changed while reading")
    return raw


def read_bytes(path: Path, max_bytes: int = MAX_BYTES) -> bytes | None:
    try:
        with _parent(path) as (fd, name):
            return _read_at(fd, name, max_bytes)
    except FileNotFoundError:
        return None


def mkdir_parents(path: Path) -> None:
    with _parent(Path(path) / ".directory-check", create=True):
        pass


def _check(fd, name, expected_hash):
    if expected_hash is not _UNSET:
        current = _read_at(fd, name, MAX_BYTES)
        actual = hashlib.sha256(current).hexdigest() if current is not None else None
        if actual != expected_hash:
            raise OSError("file changed since the reviewed preimage")


def write_bytes(path: Path, data: bytes, mode: int = 0o600, *, expected_hash=_UNSET) -> None:
    with _parent(path, create=True) as (fd, name):
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        child = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        try:
            with os.fdopen(child, "wb") as stream:
                stream.write(data)
            _check(fd, name, expected_hash)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def unlink(path: Path, *, expected_hash=_UNSET, directory: bool = False) -> None:
    with _parent(path) as (fd, name):
        _check(fd, name, expected_hash)
        (os.rmdir if directory else os.unlink)(name, dir_fd=fd)
