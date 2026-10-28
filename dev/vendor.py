#!/usr/bin/env python3
"""Vendored shelf packages: sync them from a recorded shelf tag, check them for drift.

    python3 dev/vendor.py sync NAME [--tag TAG] [--shelf DIR]   copy packages/NAME/src/NAME/ from the shelf tag into
                                                                skills/delegate/_vendor/NAME/ and rewrite dev/vendor.json
    python3 dev/vendor.py check [--shelf DIR]                   recompute every vendored tree hash; exit 1 on drift

NAME is the shelf directory or the import name (process-lock or process_lock); the vendored directory uses the import name.
src/ imports the packages by absolute name; src/delegate/__init__.py puts _vendor on sys.path (the one bootstrap).

dev/vendor.json is a list of {name, shelf_tag, shelf_commit, tree_hash}. `sync` reads the files with `git archive` from the
tag (never from the shelf working tree); with no --tag it reuses the recorded one. `check` compares the vendored directory
with the recorded hash. With --shelf (or a shelf checkout found by shelf_home) it also hashes the tag's own content and
requires the same hash, so the recorded hash is proven against the shelf.

tree_hash: sha256 over, for each file under the directory in sorted order of its POSIX relative path (skipping __pycache__
directories and *.pyc): rel_path utf-8, NUL, decimal byte length, NUL, the file bytes. Hex digest.

The vendoring workaround lives in exactly these delegate files (the vendored copy itself is byte-identical to the shelf tag:
no edits, no added files). Remove when the plugin depends on shelf directly: delete them and the _vendor/ directory.
    dev/vendor.py                        this tool
    dev/vendor.json                      the recorded tag, commit and tree hash
    src/delegate/__init__.py             the bootstrap that puts skills/delegate/_vendor on sys.path
    skills/delegate/_vendor/             the copy
    tests/test_rules.py                  VendorGuard (drift, no-edit, import-by-name) and EXEMPT_DIRS (the _vendor/ caps exemption)

Python 3.12, standard library only, POSIX only.
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "skills" / "delegate" / "_vendor"
MANIFEST = ROOT / "dev" / "vendor.json"


def tree_hash(files):
    """sha256 hex of {relative posix path: bytes}; the definition is in the module docstring."""
    h = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def dir_files(path):
    path = Path(path)
    return {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def shelf_home(explicit=None):
    """The shelf checkout: explicit, else $SHELF_HOME, else ../shelf, else ~/Workspaces/shelf; None when absent."""
    for cand in (explicit, os.environ.get("SHELF_HOME"), ROOT.parent / "shelf", Path.home() / "Workspaces" / "shelf"):
        if cand and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


def git(shelf, *args, text=True):
    return subprocess.run(["git", "-C", str(shelf), *args], check=True, capture_output=True, text=text).stdout


def tag_files(shelf, name, tag):
    """{relative path: bytes} of packages/NAME/src/NAME/ at the tag, read with git archive."""
    prefix = "packages/%s/src/%s/" % (name.replace("_", "-"), name)  # shelf dir git-porcelain, import name git_porcelain
    raw = git(shelf, "archive", tag, prefix.rstrip("/"), text=False)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if m.isfile() and m.name.startswith(prefix):
                out[m.name[len(prefix):]] = tar.extractfile(m).read()
    if not out:
        raise SystemExit("vendor: no files at %s:%s" % (tag, prefix))
    return out


def load():
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else []


def save(entries):
    MANIFEST.write_text(json.dumps(sorted(entries, key=lambda e: e["name"]), indent=2) + "\n", encoding="utf-8")


def sync(name, tag, shelf):
    name = name.replace("-", "_")  # `sync process-lock` and `sync process_lock` are one package (import name process_lock)
    entries = load()
    old = next((e for e in entries if e["name"] == name), None)
    tag = tag or (old or {}).get("shelf_tag")
    if not tag:
        raise SystemExit("vendor: no recorded tag for %s; pass --tag" % name)
    commit = git(shelf, "rev-parse", tag + "^{commit}").strip()
    files = tag_files(shelf, name, tag)
    dest = VENDOR_DIR / name
    for p in sorted(dest.rglob("*"), reverse=True) if dest.is_dir() else []:
        p.unlink() if not p.is_dir() else p.rmdir()
    if dest.is_dir():
        dest.rmdir()
    for rel, data in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    entry = {"name": name, "shelf_tag": tag, "shelf_commit": commit, "tree_hash": tree_hash(files)}
    save([e for e in entries if e["name"] != name] + [entry])
    print("vendor: %s synced from %s (%s) tree_hash %s" % (name, tag, commit[:12], entry["tree_hash"][:12]))
    return 0


def problems(shelf=None):
    """['<name>: ...'] for every entry whose vendored tree differs from the recorded hash (and, with a shelf, from the tag)."""
    out = []
    for e in load():
        path = VENDOR_DIR / e["name"]
        if not path.is_dir():
            out.append("%s: %s is missing" % (e["name"], path))
            continue
        got = tree_hash(dir_files(path))
        if got != e["tree_hash"]:
            out.append("%s: vendored tree hash %s != recorded %s" % (e["name"], got[:12], e["tree_hash"][:12]))
        if shelf:
            want = tree_hash(tag_files(shelf, e["name"], e["shelf_tag"]))
            if want != e["tree_hash"]:
                out.append("%s: tag %s content hash %s != recorded %s" % (e["name"], e["shelf_tag"], want[:12], e["tree_hash"][:12]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vendor.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("name")
    s.add_argument("--tag")
    s.add_argument("--shelf")
    c = sub.add_parser("check")
    c.add_argument("--shelf")
    args = ap.parse_args(argv)
    shelf = shelf_home(args.shelf)
    if args.cmd == "sync":
        if not shelf:
            raise SystemExit("vendor: no shelf checkout (set SHELF_HOME or pass --shelf)")
        return sync(args.name, args.tag, shelf)
    bad = problems(shelf)
    for line in bad:
        print("vendor: DRIFT " + line, file=sys.stderr)
    if not bad:
        print("vendor: ok (%d package%s%s)" % (len(load()), "" if len(load()) == 1 else "s", ", checked against the shelf tag" if shelf else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
