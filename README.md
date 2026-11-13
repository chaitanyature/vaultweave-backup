# Vaultweave

Vaultweave captures a POSIX directory as a named immutable snapshot, stores shared content once, and reconstructs the tree into a fresh location. The CLI also exports a self-contained snapshot bundle that can seed another repository offline. Python 3.10+, local Linux or macOS storage, and the standard library are sufficient at runtime.

## Run a recovery drill

```sh
python3 examples/recovery_drill.py
python3 -m unittest discover -s tests -v
```

The drill writes a seeded 6 MiB data file, a duplicate, a disposable object, and a hard-linked receipt. It inserts 551 bytes near the beginning of the dataset, takes a second snapshot, restores the first, forgets it, and collects unreachable objects. Finally it exports the surviving snapshot, imports it into a new repository, and checks the restored bytes, symlink target and inode equality against independent expectations.

| Measurement from the included fixture | Observed result |
| --- | ---: |
| Original data chunks reused after insertion | 81 of 82 |
| Bytes read from modified file | 6,292,007 |
| Additional encoded object bytes | 77,245 |
| Unreachable chunks removed | 12 |
| Regular-file bytes checked in surviving snapshot | 12,623,463 |

These are storage and correctness measurements for the seeded fixture. Throughput depends strongly on the storage device and filesystem sync behavior.

## Use the terminal

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
mkdir project
printf 'revision one\n' > project/notes.txt
vaultweave --repo vault init
vaultweave --repo vault backup project before
printf 'revision two\n' >> project/notes.txt
vaultweave --repo vault backup project after --parent before
vaultweave --repo vault diff before after
vaultweave --repo vault verify
vaultweave --repo vault pack after transport.vwp
vaultweave --repo replica init
vaultweave --repo replica unpack transport.vwp copy
vaultweave --repo replica restore copy recovered
cmp project/notes.txt recovered/notes.txt
```

Commands emit JSON on stdout and report errors on stderr with a nonzero exit status. `list` shows live snapshot names. `show NAME` emits the full manifest, including ordered chunk references and per-file digests. `forget NAME` removes one reference. `gc` previews unreachable objects; `gc --apply` reclaims them. Forgetting the last reference followed by collection makes its unique content unavailable unless it exists in another backup.

## Design decisions

- **Stable boundaries.** A normalized Gear hash selects cuts between 16 KiB and 256 KiB, with a mask change at 64 KiB. Small insertions can preserve downstream chunks. Each object is identified by SHA-256 of its original bytes. Compression is chosen only when it shrinks the payload.
- **Incremental reads.** An explicit parent can reuse a regular-file descriptor when device, inode, ctime, mtime and size match. It requires the same source root identity. Newly read chunks are verified before reuse. Cached files avoid payload reads; run `verify` for a full scrub. Full snapshots do not depend on the parent remaining live.
- **Commit ordering.** The writer syncs new immutable objects, then the manifest, then a small reference file. The reference is the publication point. An interrupted writer can leave unreachable data. Collection finds it without maintaining mutable per-chunk reference counts.
- **Restore isolation.** A private sibling directory holds the partially restored tree. All paths, directory ancestry and hardlink targets are validated before extraction. The extractor hashes both each chunk and each complete file. An exclusive directory rename publishes the result without racing a new destination into replacement.
- **Simple concurrency.** Backup, import, forget and destructive GC take one exclusive advisory lock. Inspection, export, verify and restore hold shared locks, keeping their source objects reachable while they run. GC validates live references and required object existence before deleting anything.
- **Offline replication.** A binary bundle includes the manifest, one copy of each referenced chunk and a whole-stream checksum. Import checks ordering, boundaries, cryptographic digests and the reconstructed file hashes before creating a destination reference.

## Operating envelope

The repository and staging parents must be locally trusted. SHA-256 detects accidental damage and inconsistent records; the format has no authentication, encryption, remote transport, multi-host locking, or redundant repair. Restored symbolic links keep their original targets, including explicit absolute or outside-tree links. The restore operation never traverses them. Restoring a directory atomically requires exclusive rename support from the host filesystem.

Creation-time and inode change detection catch observed races within a file read or directory traversal. A changing source tree can still represent different instants across files. Use a quiesced source or an operating-system snapshot when cross-file consistency matters. Regular files and directories preserve basic rwx permission bits and mtime. Hard links, symbolic links and empty directories are supported. Sparse extent layout, ownership, extended attributes, ACLs, symlink timestamps and special files are outside the format. Repository operations retain the manifest or live object set in RAM. A bundle manifest is limited to 128 MiB.

## Inspect the engineering

- [Durability, repository layout, and binary formats](docs/format.md)
- [Seeded restore and replication drill](examples/recovery_drill.py)
- [Filesystem and recovery failure tests](tests/test_vault.py)
- [Portable bundle integrity tests](tests/test_archive.py)

**Experiment.** Re-run the drill after replacing the near-start insertion with unrelated random content. Compare shared chunk counts and stored bytes. Then corrupt one encoded object in a disposable repository and compare `list`, `verify`, and `restore`. The catalog remains cheap to read, while full verification and restoration fail on the damaged payload.
