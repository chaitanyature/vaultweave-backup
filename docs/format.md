# Format and recovery semantics

## Directory layout

```text
vault/
  FORMAT                        format and version
  LOCK                          stable advisory lock inode
  refs/<name>                   64 ASCII hex characters naming a manifest
  manifests/<sha256>            canonical JSON, hashed as stored
  objects/<first 2>/<last 62>   content-addressed framed chunk
  tmp/write-<uuid>              unpublished or not-yet-unlinked files
```

A directory is initialized beside its final destination and published using an exclusive rename. The format document and lock file reach stable storage before publication.

## Capturing and publishing

1. Acquire the repository's exclusive flock. Check that the snapshot name is free and the source and repository do not contain each other.
2. Traverse children in sorted order with directory-relative descriptors. Symbolic links are recorded with `readlink`; opening directory and file descriptors uses no-follow flags. File descriptors and directory entries are checked against their original device, inode, mode, timestamps and size.
3. Chunk each changed file and calculate its whole-file SHA-256 while streaming. Write each unseen encoded object under a temporary name, sync it, hard-link it to its permanent name, and sync the target directory. An existing matching path is read and validated before deduplicating a fresh chunk against it.
4. Canonical JSON uses sorted keys, ASCII escapes and compact separators. The manifest includes each directory before its descendants. Each hardlink references a previously emitted regular file. Capture records root identity for explicit incremental reuse. Manifests may share data independently of their names.
5. Publish the synced manifest and its directory entry before linking the snapshot reference. Sync the refs directory before returning success. An I/O error during the last sync leaves the outcome uncertain: inspect the reference and verify it before retrying with a new name.

| Interrupted boundary | Visible effect |
| --- | --- |
| New object or manifest written, reference unpublished | Existing snapshots still point to their previous objects. A later GC can recover the unreachable space. |
| Reference linked, caller lost the response | The snapshot might already be visible. The name is exclusive and a retry cannot overwrite it. |
| Forget completed, GC interrupted | Marking restarts from all remaining roots. Anything already deleted was unreachable under the exclusive lock. |
| Restore failed before final rename | Completed destination is absent. Normal error handling removes staging files. A process kill can leave a hidden sibling staging directory. |

Durability assumes a filesystem honoring fsync, directory sync, hard links and atomic namespace operations. The killed-writer regression exercises process crash recovery. Device write-cache failures and power cuts require separate hardware-specific testing.

## Chunk objects

Fields appear in network byte order. The fixed 9-byte header stores `VW01`, an unsigned codec byte (0 raw, 1 zlib), and an unsigned 32-bit original length. The file name hashes the original bytes. The decoder bounds encoded input, caps decompression, rejects trailing compressed streams, and checks the decoded length and digest. Each file manifest contains ordered pairs of digest and original byte count, which must cover its size exactly. A separate whole-file digest guards against substitution, omission, or reordering of otherwise valid chunks.

## Snapshot bundles

| Field | Encoding |
| --- | --- |
| Magic | Eight bytes, `VWPACK01` |
| Manifest | u32 byte count, 32 raw digest bytes, JSON bytes |
| Inventory | u32 distinct chunk count |
| Each object in ascending digest order | 32 raw digest bytes, u32 encoded length, chunk object including its header |
| Trailer | 32-byte SHA-256 of all preceding bundle bytes, then EOF |

An importer compares IDs with the manifest-derived inventory. Bundle header and frame lengths are bounded before allocating. Failed import may leave valid unreachable chunks; snapshot publication occurs only after full verification. A scope-limited scanner could use the same format without extracting links or creating filesystem paths.

## Collection and cost

`gc` marks from every catalog reference. It fails before deletion for an unreadable manifest or a missing required chunk. Then it enumerates objects, manifests and temporary artifacts to calculate reclamation. It holds its lock through the sweep and syncs directories whose entries changed. Collection checks reachability and existence. `verify` reads and verifies the actual payloads.

Capture with a parent still enumerates all entries and stats every cached file's chunk paths. The current full-scrub and export paths may read shared chunks repeatedly to verify each independent file digest. This deliberately trades throughput for complete content-order validation with bounded chunk buffers.
