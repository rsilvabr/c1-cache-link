#!/usr/bin/env python3
"""c1_cache_link - keep Capture One's Cache folders on a fast disk.

Capture One keeps, next to the photos, a `CaptureOne` folder with the edits
(`Settings*`: .cos/.comask) and a `Cache` folder (Thumbnails .cot, Proxies
.cof/.cop) that it can always rebuild. On a hard disk the cache makes browsing
slow. This script replaces each `...\\CaptureOne\\Cache` with an NTFS
JUNCTION to a folder on a fast disk, so Capture One reads and writes the cache
there while the edits stay with the photos.

Commands (nothing changes without --apply; every run lists what it would do):

  status   list every Capture One folder and the state of its Cache; writes
           the map (a text file of where each cache lives).
  link     real Cache folder -> junction to CACHE_ROOT (or --to). The
           thumbnails are moved there; --discard drops them instead (Capture
           One rebuilds what it opens). A CaptureOne folder without a Cache
           gets an empty one on the fast disk.
  move     --to NEW_ROOT: every junction found is pointed at NEW_ROOT, the
           cache moved along (--discard: not moved). A junction whose target
           is gone (the fast disk was replaced or wiped) gets an EMPTY folder
           at NEW_ROOT - `move --to <the same root>` is the repair.
  unlink   junction -> real Cache folder back next to the photos, the cache
           moved back (--discard: an empty folder, the copy on the fast disk
           is deleted).

The junctions are the record of what is linked: every command finds them by
scanning the --source folders. The map (MAP_FILE_NAME in the cache root, plus
a copy at MAP_COPY) is a readable list of the same thing.

Only folders named Cache directly inside a CaptureOne folder are touched.
Capture One must be closed (--apply refuses while it runs).
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field

VERSION = "0.1.0"

# ===== Settings (edit here; the command-line flags override them) ==========
CACHE_ROOT = r"E:\C1_Cache"         # where the caches live (--to)
SOURCE_ROOTS = "G:\\"                # ';'-separated folders scanned (--source)
CAPTUREONE_FOLDER_NAMES = "CaptureOne"   # ';'-separated, any case
CACHE_FOLDER_NAME = "Cache"          # the folder inside CaptureOne that is linked
MAP_FILE_NAME = "_c1_cache_map.tsv"  # written in the cache root
MAP_COPY = "~/.c1_cache_map.tsv"     # second copy, survives losing the fast disk ("" = none)
SKIP_FOLDER_NAMES = "$RECYCLE.BIN;System Volume Information"   # never scanned
SPACE_MARGIN_GB = 10.0               # free space kept on the destination disk
CAPTURE_ONE_PROCESS_NAMES = "CaptureOne.exe"   # ';'-separated; --apply refuses while one runs
STAGING_SUFFIX = ".c1link-moving"    # old real Cache while it is being moved away
RETURNING_SUFFIX = ".c1link-returning"   # cache being moved back by unlink
PARTIAL_SUFFIX = ".c1link-partial"   # copy in progress on the destination
PROGRESS_SECONDS = 2.0               # how often the scan reports progress
# =============================================================================


def _split(value):
    return [part.strip() for part in value.split(";") if part.strip()]


def _fold_set(value):
    return frozenset(part.casefold() for part in _split(value))


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _gb(nbytes):
    return f"{nbytes / 1024 ** 3:.2f} GB"


def _strip_prefix(path):
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\") or path.startswith("\\??\\"):
        return path[4:]
    return path


def is_link(path):
    """True for a junction or a symlink (never followed by this script)."""
    try:
        return os.path.isjunction(path) or os.path.islink(path)
    except OSError:
        return False


def link_target(path):
    return os.path.normpath(_strip_prefix(os.readlink(path)))


def create_junction(target, link):
    """Directory junction `link` -> `target` (works across local volumes)."""
    if os.name != "nt":
        raise OSError("junctions exist only on Windows")
    import _winapi
    _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))


def remove_junction(link):
    """Removes the junction itself; the folder it points to is untouched."""
    if not is_link(link):
        raise OSError(f"not a junction: {link}")
    os.rmdir(link)


def _onexc(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc


def rmtree(path):
    if is_link(path):
        os.rmdir(path)
    else:
        shutil.rmtree(path, onexc=_onexc)


def tree_files(root):
    """{relative path: size} of every file below root (links not followed)."""
    found = {}
    stack = [root]
    while stack:
        path = stack.pop()
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    found[os.path.relpath(entry.path, root)] = entry.stat(follow_symlinks=False).st_size
    return found


def tree_bytes(root):
    try:
        return sum(tree_files(root).values())
    except OSError:
        return 0


def copy_tree(src, dst):
    """Copies src into the new folder dst and checks every file and size."""
    files = tree_files(src)
    os.makedirs(dst)
    for rel in sorted(files):
        target = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(src, rel), target)
    if tree_files(dst) != files:
        raise OSError(f"copy check failed: {dst} differs from {src}")
    return len(files), sum(files.values())


def capture_one_running():
    names = _fold_set(CAPTURE_ONE_PROCESS_NAMES)
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, errors="replace", timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        first = line.split(",", 1)[0].strip().strip('"').casefold()
        if first in names:
            return first
    return ""


def free_bytes(path):
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def _drive_key(path):
    return os.path.normcase(os.path.splitdrive(os.path.abspath(path))[0])


def mirror(root, cache_path):
    """Where a cache lives below root: the drive letter, then the same path."""
    drive, rest = os.path.splitdrive(os.path.abspath(cache_path))
    label = drive.replace(":", "").strip("\\/").replace("\\", "_").replace("/", "_")
    return os.path.join(os.path.abspath(root), label or "_", rest.lstrip("\\/"))


def _is_cache_folder_path(path):
    """A folder this script may delete: <...>\\CaptureOne\\Cache, not a link."""
    parent = os.path.dirname(os.path.normpath(path))
    return (os.path.basename(os.path.normpath(path)).casefold() == CACHE_FOLDER_NAME.casefold()
            and os.path.basename(parent).casefold() in _fold_set(CAPTUREONE_FOLDER_NAMES))


def _clear_cache_folder(path):
    if os.path.lexists(path):
        if not _is_cache_folder_path(_strip_partial(path)):
            raise OSError(f"refusing to delete a folder that is not a Capture One cache: {path}")
        rmtree(path)


def _strip_partial(path):
    return path[:-len(PARTIAL_SUFFIX)] if path.endswith(PARTIAL_SUFFIX) else path


def prune_empty_parents(path):
    """Removes the folders left empty above a deleted cache, up to the cache root."""
    parent = os.path.dirname(os.path.normpath(path))
    while parent and os.path.dirname(parent) != parent:
        if os.path.exists(os.path.join(parent, MAP_FILE_NAME)):
            return
        try:
            if os.listdir(parent):
                return
            os.rmdir(parent)
        except OSError:
            return
        parent = os.path.dirname(parent)


# --- scan ---------------------------------------------------------------------

@dataclass
class Item:
    c1_dir: str
    cache: str
    state: str = ""            # real | linked | broken | none | odd
    target: str = ""
    nbytes: int = 0
    leftovers: list = field(default_factory=list)

    @property
    def staging(self):
        return self.cache + STAGING_SUFFIX

    @property
    def returning(self):
        return self.cache + RETURNING_SUFFIX


def classify(item, measure):
    cache = item.cache
    item.leftovers = [p for p in (item.staging, item.returning) if os.path.lexists(p)]
    if is_link(cache):
        item.target = link_target(cache)
        item.state = "linked" if os.path.isdir(item.target) else "broken"
        if measure and item.state == "linked":
            item.nbytes = tree_bytes(item.target)
    elif os.path.isdir(cache):
        item.state = "real"
        if measure:
            item.nbytes = tree_bytes(cache)
    elif os.path.lexists(cache):
        item.state = "odd"
    else:
        item.state = "none"
    return item


def scan(sources, skip_roots=(), measure=True, quiet=False):
    c1_names = _fold_set(CAPTUREONE_FOLDER_NAMES)
    skip_names = _fold_set(SKIP_FOLDER_NAMES)
    skip = {os.path.normcase(os.path.abspath(p)) for p in skip_roots}
    items, errors = [], []
    seen_dirs = 0
    last = time.monotonic()
    stack = [os.path.abspath(s) for s in reversed(sources)]
    while stack:
        path = stack.pop()
        if os.path.normcase(path) in skip or os.path.exists(os.path.join(path, MAP_FILE_NAME)):
            continue
        seen_dirs += 1
        if not quiet and time.monotonic() - last >= PROGRESS_SECONDS:
            print(f"  scanning... {seen_dirs} folders, {len(items)} Capture One folders found",
                  flush=True)
            last = time.monotonic()
        try:
            with os.scandir(path) as entries:
                subdirs = []
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False) or entry.is_junction():
                            continue
                    except OSError:
                        continue
                    folded = entry.name.casefold()
                    if folded in skip_names:
                        continue
                    if folded in c1_names:
                        items.append(Item(entry.path,
                                          os.path.join(entry.path, CACHE_FOLDER_NAME)))
                    else:
                        subdirs.append(entry.path)
        except OSError as exc:
            errors.append(f"cannot read {path}: {exc}")
            continue
        stack.extend(sorted(subdirs, reverse=True))
    for item in items:
        try:
            classify(item, measure)
        except OSError as exc:
            item.state = "odd"
            errors.append(f"cannot read {item.cache}: {exc}")
    return items, errors


# --- map ----------------------------------------------------------------------

def write_map(items, cache_roots):
    lines = [f"# c1_cache_link {VERSION} map, written {time.strftime('%Y-%m-%d %H:%M:%S')}",
             "# state\tcache folder next to the photos\twhere it lives (junction target)"]
    for item in sorted(items, key=lambda i: i.cache.casefold()):
        lines.append(f"{item.state}\t{item.cache}\t{item.target}")
    text = "\n".join(lines) + "\n"
    written = []
    targets = [os.path.join(root, MAP_FILE_NAME) for root in cache_roots]
    if MAP_COPY:
        targets.append(os.path.expanduser(MAP_COPY))
    for path in targets:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(text)
            os.replace(tmp, path)
            written.append(path)
        except OSError as exc:
            print(f"  WARNING: map not written to {path}: {exc}")
    return written


# --- operations (each one changes one Capture One folder) ---------------------

def recover_leftovers(item):
    """Undoes what an interrupted run left behind. The staging/returning
    folders only ever hold cache: one is put back as Cache when Cache is gone,
    otherwise it is deleted."""
    notes = []
    if os.path.lexists(item.returning):
        rmtree(item.returning)
        notes.append("removed an unfinished move back")
    if os.path.lexists(item.staging):
        if not os.path.lexists(item.cache):
            os.rename(item.staging, item.cache)
            notes.append("restored the Cache an interrupted run had set aside")
        else:
            rmtree(item.staging)
            notes.append("removed the old cache an interrupted run had left")
    classify(item, measure=False)
    return notes


def op_link(item, root, discard):
    target = mirror(root, item.cache)
    partial = target + PARTIAL_SUFFIX
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _clear_cache_folder(partial)
    _clear_cache_folder(target)
    if item.state == "none":
        os.makedirs(target)
        create_junction(target, item.cache)
        return "empty cache linked"
    os.rename(item.cache, item.staging)
    try:
        if discard:
            os.makedirs(target)
        else:
            copy_tree(item.staging, partial)
            os.rename(partial, target)
        create_junction(target, item.cache)
        if not (is_link(item.cache) and _same(link_target(item.cache), target)):
            raise OSError("the junction does not point where it should")
    except BaseException:
        if is_link(item.cache):
            os.rmdir(item.cache)
        if not os.path.lexists(item.cache):
            os.rename(item.staging, item.cache)
        for leftover in (partial, target):
            try:
                _clear_cache_folder(leftover)
            except OSError:
                pass
        raise
    rmtree(item.staging)
    return "linked (old cache discarded)" if discard else "moved and linked"


def op_move(item, root, discard):
    old = item.target
    new = mirror(root, item.cache)
    if _same(old, new):
        if os.path.isdir(new):
            return None
        os.makedirs(new)
        return "repaired: empty cache recreated"
    partial = new + PARTIAL_SUFFIX
    os.makedirs(os.path.dirname(new), exist_ok=True)
    _clear_cache_folder(partial)
    old_exists = os.path.isdir(old)
    if not os.path.isdir(new):
        if old_exists and not discard:
            copy_tree(old, partial)
            os.rename(partial, new)
        else:
            os.makedirs(new)
    remove_junction(item.cache)
    try:
        create_junction(new, item.cache)
    except BaseException:
        create_junction(old, item.cache)
        raise
    if old_exists and _is_cache_folder_path(old):
        rmtree(old)
        prune_empty_parents(old)
    if not old_exists:
        return "relinked (old cache was gone: empty)"
    return "relinked (old cache discarded)" if discard else "moved and relinked"


def op_unlink(item, discard):
    old = item.target
    old_exists = os.path.isdir(old)
    if old_exists and not discard:
        copy_tree(old, item.returning)
    else:
        os.makedirs(item.returning)
    remove_junction(item.cache)
    try:
        os.rename(item.returning, item.cache)
    except BaseException:
        create_junction(old, item.cache)
        raise
    if old_exists and _is_cache_folder_path(old):
        rmtree(old)
        prune_empty_parents(old)
    if discard or not old_exists:
        return "real folder again (empty)"
    return "moved back"


# --- command line -------------------------------------------------------------

def _sources(args):
    found = []
    for value in args.source or [SOURCE_ROOTS]:
        found.extend(_split(value))
    return [os.path.abspath(s) for s in found]


def _print_summary(items, errors):
    counts = {}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    real_bytes = sum(i.nbytes for i in items if i.state == "real")
    linked_bytes = sum(i.nbytes for i in items if i.state == "linked")
    print(f"Capture One folders: {len(items)}")
    print(f"  real Cache next to the photos : {counts.get('real', 0):5d}   {_gb(real_bytes)}")
    print(f"  linked to a fast disk         : {counts.get('linked', 0):5d}   {_gb(linked_bytes)}")
    print(f"  link whose target is gone     : {counts.get('broken', 0):5d}   "
          "(repair: move --to <cache root>)")
    print(f"  no Cache folder yet           : {counts.get('none', 0):5d}")
    if counts.get("odd"):
        print(f"  Cache is a file / unreadable  : {counts['odd']:5d}   (left alone)")
    roots = {}
    for item in items:
        if item.state in ("linked", "broken"):
            key = os.path.normcase(os.path.splitdrive(item.target)[0])
            roots[key] = roots.get(key, 0) + 1
    for drive, count in sorted(roots.items()):
        print(f"  junctions pointing to {drive.upper() or '?'}        : {count:5d}")
    leftovers = [p for i in items for p in i.leftovers]
    if leftovers:
        print(f"  left by an interrupted run    : {len(leftovers):5d}   (the next link/move/unlink cleans them)")
    for item in items:
        if item.state == "broken":
            print(f"    gone: {item.target}  <- {item.cache}")
    for error in errors:
        print(f"  ERROR: {error}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="c1_cache_link",
                                     description="Keep Capture One's Cache folders on a fast disk (NTFS junctions).")
    parser.add_argument("command", choices=("status", "link", "move", "unlink"))
    parser.add_argument("--source", action="append", metavar="PATH",
                        help=f"folder(s) to scan, ';'-separated or repeated (default {SOURCE_ROOTS})")
    parser.add_argument("--to", metavar="ROOT", default=None,
                        help=f"cache root for link/move (default {CACHE_ROOT})")
    parser.add_argument("--discard", action="store_true",
                        help="do not move the existing thumbnails; Capture One rebuilds them")
    parser.add_argument("--apply", action="store_true",
                        help="really change things (without it: a dry run)")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.to or CACHE_ROOT)
    if args.command == "move" and not args.to:
        print("move needs --to <new cache root> (the same root repairs gone targets)")
        return 2
    sources = _sources(args)
    for src in sources:
        if not os.path.isdir(src):
            print(f"source not found: {src}")
            return 2
    print(f"c1_cache_link {VERSION} - {args.command}"
          f"{'' if args.apply or args.command == 'status' else '  (DRY RUN: nothing changes; add --apply)'}")
    print(f"Sources:    {'; '.join(sources)}")
    if args.command in ("link", "move"):
        print(f"Cache root: {root}")
    if args.command in ("link", "move") and any(_drive_key(s) == _drive_key(root) for s in sources):
        print("  note: the cache root is on the same disk as the photos - no speed gain")

    t0 = time.monotonic()
    items, errors = scan(sources, skip_roots=[root])
    print(f"  scan: {len(items)} Capture One folders in {time.monotonic() - t0:.0f} s")
    _print_summary(items, errors)

    if args.command == "status":
        written = write_map(items, [root] if os.path.isdir(root) else [])
        for path in written:
            print(f"Map: {path}")
        return 1 if errors else 0

    if args.command == "link":
        todo = [i for i in items if i.state in ("real", "none") or i.leftovers]
        need = 0 if args.discard else sum(i.nbytes for i in todo if i.state == "real")
        free_drive = root
    elif args.command == "move":
        todo = [i for i in items if i.state in ("linked", "broken")]
        need = 0 if args.discard else sum(i.nbytes for i in todo
                                          if i.state == "linked" and not _same(i.target, mirror(root, i.cache)))
        free_drive = root
    else:
        todo = [i for i in items if i.state in ("linked", "broken")]
        need = 0 if args.discard else sum(i.nbytes for i in todo if i.state == "linked")
        free_drive = sources[0]

    margin = int(SPACE_MARGIN_GB * 1024 ** 3)
    free = free_bytes(free_drive)
    if args.command == "unlink":
        by_drive = {}
        for item in todo:
            if item.state == "linked" and not args.discard:
                by_drive[_drive_key(item.cache)] = by_drive.get(_drive_key(item.cache), 0) + item.nbytes
        fits = all(free_bytes(drive + "\\") - margin >= n for drive, n in by_drive.items())
    else:
        fits = need == 0 or free - margin >= need
    print(f"To do: {len(todo)} Capture One folders, {_gb(need)} to move"
          f"{' (thumbnails discarded)' if args.discard else ''}")
    print(f"Space: need {_gb(need)}, free {_gb(free)} on {_drive_key(free_drive).upper()}, "
          f"margin {SPACE_MARGIN_GB:g} GB -> {'fits' if fits else 'DOES NOT FIT'}")
    if not fits:
        print("Refused: not enough space. Use --discard (Capture One rebuilds the thumbnails it "
              "needs) or pick fewer --source folders.")
        return 2
    if not todo:
        print("Nothing to do.")
        if args.apply:
            write_map(items, [root])
        return 0
    if not args.apply:
        for item in todo[:20]:
            dest = mirror(root, item.cache) if args.command != "unlink" else item.cache
            print(f"  {item.state:6s} {_gb(item.nbytes):>10s}  {item.cache}  ->  {dest}")
        if len(todo) > 20:
            print(f"  ... and {len(todo) - 20} more")
        print("Dry run: nothing changed. Close Capture One and add --apply to do it.")
        return 0

    running = capture_one_running()
    if running is None:
        print("Refused: could not check whether Capture One is running (tasklist failed).")
        return 2
    if running:
        print(f"Refused: Capture One is running ({running}). Close it first.")
        return 2

    failures = 0
    done_bytes = 0
    t0 = time.monotonic()
    for n, item in enumerate(todo, 1):
        try:
            notes = recover_leftovers(item) if item.leftovers else []
            if args.command == "link":
                if item.state not in ("real", "none"):
                    result = None
                else:
                    result = op_link(item, root, args.discard)
            elif args.command == "move":
                result = op_move(item, root, args.discard)
            else:
                result = op_unlink(item, args.discard)
            done_bytes += item.nbytes
            elapsed = time.monotonic() - t0
            eta = ""
            if need and done_bytes and not args.discard:
                eta = f", ~{(need - done_bytes) * elapsed / done_bytes / 60:.0f} min left"
            message = "; ".join(notes + ([result] if result else [])) or "nothing to do"
            print(f"[{n}/{len(todo)}] {message}: {item.cache} ({_gb(item.nbytes)}{eta})", flush=True)
        except (OSError, shutil.Error) as exc:
            failures += 1
            print(f"[{n}/{len(todo)}] FAILED, left as it was: {item.cache}: {exc}", flush=True)
        try:
            classify(item, measure=False)
        except OSError:
            pass
    for item in items:
        if item.state in ("linked", "broken"):
            try:
                item.target = link_target(item.cache)
            except OSError:
                pass
    for path in write_map(items, [root]):
        print(f"Map: {path}")
    print(f"Done in {(time.monotonic() - t0) / 60:.1f} min: {len(todo) - failures} ok, {failures} failed.")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
