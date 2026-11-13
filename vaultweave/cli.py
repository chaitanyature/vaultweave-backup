import argparse
import json
import sys

from .archive import pack, unpack
from .capture import capture
from .recover import diff, restore, verify
from .repository import Repository, initialize


def entry(argv=None):
    p = argparse.ArgumentParser(
        prog="vaultweave",
        description="Snapshot backup, verified restore and storage reclamation",
    )
    p.add_argument("--repo", required=True, help="repository directory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    b = sub.add_parser("backup")
    b.add_argument("source")
    b.add_argument("name")
    b.add_argument("--parent", help="reuse unchanged regular files from this snapshot")
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("name")
    d = sub.add_parser("diff")
    d.add_argument("first")
    d.add_argument("second")
    v = sub.add_parser("verify")
    v.add_argument("name", nargs="?")
    r = sub.add_parser("restore")
    r.add_argument("name")
    r.add_argument("destination")
    f = sub.add_parser("forget")
    f.add_argument("name")
    export = sub.add_parser("pack", help="write a self-contained portable snapshot")
    export.add_argument("name")
    export.add_argument("archive")
    load = sub.add_parser("unpack", help="verify and import a portable snapshot")
    load.add_argument("archive")
    load.add_argument("name")
    g = sub.add_parser("gc")
    g.add_argument(
        "--apply",
        action="store_true",
        help="delete unreachable objects selected by the collector",
    )
    args = p.parse_args(argv)
    try:
        if args.cmd == "init":
            result = {"repository": str(initialize(args.repo)), "format": 1}
        else:
            repo = Repository(args.repo)
            exclusive = args.cmd in ("backup", "forget", "unpack") or (
                args.cmd == "gc" and args.apply
            )
            with repo.session(write=exclusive):
                if args.cmd == "backup":
                    result = capture(repo, args.source, args.name, args.parent)
                elif args.cmd == "list":
                    result = repo.names()
                elif args.cmd == "show":
                    result = repo.manifest(args.name)[1]
                elif args.cmd == "diff":
                    result = diff(repo, args.first, args.second)
                elif args.cmd == "verify":
                    result = verify(repo, args.name)
                elif args.cmd == "restore":
                    result = restore(repo, args.name, args.destination)
                elif args.cmd == "pack":
                    result = pack(repo, args.name, args.archive)
                elif args.cmd == "unpack":
                    result = unpack(repo, args.archive, args.name)
                elif args.cmd == "forget":
                    repo.forget(args.name)
                    result = {"forgotten": args.name}
                else:
                    result = repo.collect(args.apply)
        print(json.dumps(result, sort_keys=True, indent=2))
    except (OSError, ValueError, RecursionError) as exc:
        sys.exit(f"vaultweave: {exc}")
