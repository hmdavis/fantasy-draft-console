#!/usr/bin/env python3
"""Copy this league's LOCAL data in and out of one folder, for syncing between machines.

Everything league-specific is gitignored by design (see .gitignore), which keeps the repo
publishable but means a fresh clone on a second laptop has no league in it. This gathers
those paths into a single directory you can put in iCloud / Dropbox / a USB stick, and
restores them on the other side.

    python3 sync_league_data.py export ~/iCloud/fantasy-league     # repo  -> folder
    python3 sync_league_data.py import ~/iCloud/fantasy-league     # folder -> repo

SECRETS ARE EXCLUDED BY DEFAULT. config/.env (your ANTHROPIC_API_KEY),
scraping/.espn_auth.json (live ESPN session cookies) and api_key.txt (your FantasyPros
key) are skipped unless you pass --with-secrets. Only add that flag if the destination
is private to YOU — a shared cloud folder is not. Credentials are cheap to retype and
expensive to leak; ESPN cookies expire every few weeks anyway.

Use --dry-run to see what would move without touching anything.
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# league-specific paths worth carrying between machines (all gitignored)
PATHS = [
    "config",                        # league.json, briefing.md, tendencies, manager_canon
    "scraping/raw",                  # the scraped history — the expensive part to rebuild
    "scraping/.espn_auth.json",      # secret: listed so --with-secrets can reach it
    "draft_sheets/tool_data.json",   # generated console payload
    "draft_sheets/elboberto_projections.json",
    "leagues",
    "draft_sheets/csg_consensus.json",
    "out/board/history.json",
    "api_key.txt",
]

SECRETS = {"config/.env", "scraping/.espn_auth.json", "api_key.txt"}


def _rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def _iter_files(src_root, rel):
    """Yield repo-relative file paths under `rel` (which may be a file or a directory)."""
    full = os.path.join(src_root, rel)
    if os.path.isfile(full):
        yield rel
    elif os.path.isdir(full):
        for dirpath, _dirs, files in os.walk(full):
            for f in files:
                p = os.path.join(dirpath, f)
                yield os.path.relpath(p, src_root).replace("\\", "/")


def move(src_root, dst_root, with_secrets, dry_run):
    n = bytes_moved = skipped = 0
    for rel in PATHS:
        for f in _iter_files(src_root, rel):
            if f in SECRETS and not with_secrets:
                print("  skip (secret)  %s" % f)
                skipped += 1
                continue
            src, dst = os.path.join(src_root, f), os.path.join(dst_root, f)
            size = os.path.getsize(src)
            if not dry_run:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            n += 1
            bytes_moved += size
    print("\n%s %d files (%.1f MB)%s"
          % ("would copy" if dry_run else "copied", n, bytes_moved / 1e6,
             ", %d secret(s) skipped" % skipped if skipped else ""))
    if skipped:
        print("Recreate those by hand on the other machine, or re-run with --with-secrets "
              "if the destination is private to you.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("direction", choices=["export", "import"],
                    help="export: repo -> folder.  import: folder -> repo.")
    ap.add_argument("folder", help="the sync folder (e.g. an iCloud/Dropbox path)")
    ap.add_argument("--with-secrets", action="store_true",
                    help="also copy config/.env, scraping/.espn_auth.json and api_key.txt")
    ap.add_argument("--dry-run", action="store_true", help="show what would move")
    args = ap.parse_args()

    folder = os.path.abspath(os.path.expanduser(args.folder))
    if args.direction == "export":
        src, dst = ROOT, folder
        if not args.dry_run:
            os.makedirs(folder, exist_ok=True)
    else:
        src, dst = folder, ROOT
        if not os.path.isdir(folder):
            sys.exit("No such folder: %s" % folder)

    print("%s  %s\n  ->  %s\n" % (args.direction, src, dst))
    move(src, dst, args.with_secrets, args.dry_run)


if __name__ == "__main__":
    main()
