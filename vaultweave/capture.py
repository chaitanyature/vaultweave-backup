"""Capture by descriptor-relative traversal. A detected race aborts publication."""

import hashlib
import os
from pathlib import Path
import stat

from .chunks import chunks
from .manifest import by_path


class Changed(OSError):
    pass


def signature(st):
    return [st.st_dev, st.st_ino, st.st_ctime_ns]


def fingerprint(st):
    return (*signature(st), st.st_mtime_ns, st.st_mode, st.st_size)


def check_stable(before, after, name):
    if fingerprint(before) != fingerprint(after):
        raise Changed(f"source changed while reading {name!r}; retry backup")


def metadata(st):
    return {"mode": stat.S_IMODE(st.st_mode) & 0o777, "mtime_ns": st.st_mtime_ns}


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def capture(repo, source, name, parent=None):
    if repo.ref_path(name).exists():
        raise FileExistsError(f"snapshot already exists: {name}")
    source = Path(source).resolve(strict=True)
    if overlaps(source, repo.root):
        raise ValueError("source and repository directory trees must be disjoint")
    root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    entries = []
    stats = {
        "files_read": 0,
        "files_reused": 0,
        "hardlinks": 0,
        "logical_file_bytes": 0,
        "new_chunks": 0,
        "new_stored_bytes": 0,
        "bytes_read": 0,
    }
    try:
        root_stat = os.fstat(root)
        source_id = [root_stat.st_dev, root_stat.st_ino]
        cached = {}
        if parent:
            _, old = repo.manifest(parent)
            if old["source"] != source_id:
                raise ValueError(
                    "incremental parent belongs to another source directory"
                )
            cached = by_path(old["entries"])
        inodes = {}
        visited_dirs = set()

        def directory(fd, rel):
            before = os.fstat(fd)
            identity = (before.st_dev, before.st_ino)
            if identity in visited_dirs:
                raise ValueError(f"repeated directory inode at {rel!r}")
            visited_dirs.add(identity)
            entries.append({"type": "dir", "path": rel, **metadata(before)})
            for child in sorted(os.listdir(fd)):
                path = rel + "/" + child if rel else child
                initial = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(initial.st_mode):
                    child_fd = os.open(
                        child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        check_stable(initial, os.fstat(child_fd), path)
                        directory(child_fd, path)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISLNK(initial.st_mode):
                    target = os.readlink(child, dir_fd=fd)
                    entries.append({"path": path, "type": "symlink", "target": target})
                elif stat.S_ISREG(initial.st_mode):
                    ident = (initial.st_dev, initial.st_ino)
                    stats["logical_file_bytes"] += initial.st_size
                    if ident in inodes:
                        entries.append(
                            {"path": path, "type": "hardlink", "target": inodes[ident]}
                        )
                        stats["hardlinks"] += 1
                    else:
                        entry = regular(fd, child, path, initial, cached.get(path))
                        entries.append(entry)
                        inodes[ident] = path
                else:
                    raise ValueError(f"unsupported special file: {path!r}")
                check_stable(
                    initial, os.stat(child, dir_fd=fd, follow_symlinks=False), path
                )
            check_stable(before, os.fstat(fd), rel)

        def regular(parent_fd, child, path, st, prior):
            sig = signature(st)
            if (
                prior
                and prior["type"] == "file"
                and prior["signature"] == sig
                and prior["size"] == st.st_size
                and prior["mtime_ns"] == st.st_mtime_ns
            ):
                for key, _ in prior["chunks"]:
                    if not repo.object_path(key).is_file():
                        raise ValueError(
                            f"incremental parent missing chunk {key}; run verify"
                        )
                stats["files_reused"] += 1
                return prior.copy()
            refs = []
            hashfile = hashlib.sha256()
            # NONBLOCK prevents a regular-file-to-FIFO race from hanging the scanner.
            fd = os.open(
                child, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
            )
            with os.fdopen(fd, "rb") as f:
                check_stable(st, os.fstat(fd), path)
                for raw in chunks(f):
                    hashfile.update(raw)
                    key, written = repo.store_chunk(raw)
                    refs.append([key, len(raw)])
                    stats["bytes_read"] += len(raw)
                    if written:
                        stats["new_chunks"] += 1
                        stats["new_stored_bytes"] += written
                check_stable(st, os.fstat(fd), path)
            stats["files_read"] += 1
            return {
                "path": path,
                "type": "file",
                "size": st.st_size,
                "sha256": hashfile.hexdigest(),
                "signature": sig,
                "chunks": refs,
                **metadata(st),
            }

        directory(root, "")
        check_stable(root_stat, source.stat(), "source root")
        manifest = {"version": 1, "source": source_id, "entries": entries}
        stats.update(
            snapshot=name, manifest=repo.commit(name, manifest), entries=len(entries)
        )
        return stats
    finally:
        os.close(root)
