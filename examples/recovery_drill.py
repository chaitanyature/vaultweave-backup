"""Generate two generations, verify deduplication and restore through the public CLI."""

import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run():
    with tempfile.TemporaryDirectory() as work:
        base = Path(work)
        repo = base / "vault"
        live = base / "live"
        live.mkdir()

        def cli(*args):
            result = subprocess.run(
                [sys.executable, "-m", "vaultweave", "--repo", repo, *args],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            if result.returncode:
                raise RuntimeError(result.stderr)
            return json.loads(result.stdout)

        source = random.Random(5021).randbytes(6 * 1024 * 1024)
        (live / "dataset.bin").write_bytes(source)
        (live / "duplicate.bin").write_bytes(source)
        (live / "obsolete.bin").write_bytes(random.Random(83).randbytes(1024 * 1024))
        (live / "receipts").mkdir()
        (live / "receipts" / "index.txt").write_text("settled\n" * 5000)
        os.link(live / "receipts" / "index.txt", live / "index-copy")
        (live / "latest").symlink_to("receipts/index.txt")
        cli("init")
        first = cli("backup", live, "monday")
        manifest = cli("show", "monday")

        def file_chunks(m):
            return {
                obj
                for e in m["entries"]
                if e["path"] == "dataset.bin"
                for obj, _ in e["chunks"]
            }

        a = file_chunks(manifest)
        changed = source[:10000] + b"upstream revision!\n" * 29 + source[10000:]
        (live / "dataset.bin").write_bytes(changed)
        (live / "obsolete.bin").unlink()
        second = cli("backup", live, "tuesday", "--parent", "monday")
        b = file_chunks(cli("show", "tuesday"))
        common = len(a & b)
        assert common > len(a) * 0.8 and second["files_reused"] >= 2
        print(f"6 MiB edit: {common}/{len(a)} original content chunks reused")
        print(
            f"Incremental scan: {second['bytes_read']:,} bytes read; {second['new_stored_bytes']:,} encoded bytes added"
        )
        change = cli("diff", "monday", "tuesday")
        assert {"path": "dataset.bin", "change": "content"} in change and {
            "path": "obsolete.bin",
            "change": "removed",
        } in change
        cli("restore", "monday", base / "old")
        assert (base / "old" / "dataset.bin").read_bytes() == source
        cli("forget", "monday")
        preview = cli("gc")
        freed = cli("gc", "--apply")
        assert freed["bytes"] == preview["bytes"] and freed["chunks"] > 0
        print(
            f"Reclamation: {freed['chunks']} unreachable chunks; {freed['bytes']:,} bytes including obsolete manifests"
        )
        result = cli("verify")
        transport = base / "offline.vwp"
        packed = cli("pack", "tuesday", transport)
        repo = base / "replica"
        cli("init")
        imported = cli("unpack", transport, "tuesday")
        assert packed["manifest"] == imported["manifest"] == second["manifest"]
        dst = base / "recovered"
        cli("restore", "tuesday", dst)
        assert (dst / "dataset.bin").read_bytes() == changed
        assert (dst / "duplicate.bin").read_bytes() == source
        assert not (dst / "obsolete.bin").exists()
        assert (dst / "index-copy").samefile(dst / "receipts" / "index.txt")
        assert os.readlink(dst / "latest") == "receipts/index.txt"
        print(
            f"Verified {result['verified_bytes']:,} bytes and link topology after offline replication. Restored dataset SHA-256: {hashlib.sha256(changed).hexdigest()}"
        )
        return first, second


if __name__ == "__main__":
    run()
