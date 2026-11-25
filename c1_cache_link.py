#!/usr/bin/env python3
"""c1_cache_link - keep Capture One's Cache folders on a fast disk.

Capture One keeps, next to the photos, a `CaptureOne` folder with the edits
(`Settings*`: .cos/.comask) and a `Cache` folder (Thumbnails .cot, Proxies
.cof/.cop) that it can always rebuild. On a hard disk the cache makes browsing
slow. This script replaces each `...\\CaptureOne\\Cache` with an NTFS
JUNCTION to a folder on a fast disk, so Capture One reads and writes the cache
there while the edits stay with the photos.

Commands (nothing changes without --apply; every run lists what it would do):

  status   list every Capture One folder and the state of its Cache, and the
           plan per rule (how many folders and GB go where); writes the map (a
           text file of where each cache lives). Changes nothing else.
  link     reconciles every Capture One folder with the RULES (the rules file,
           RULES_FILE or --rules; without one: every cache goes to CACHE_ROOT
           or --to). A real Cache is moved to its rule's root and linked there;
           a rule that says "none" brings the cache back beside the photos; a
           junction whose rule changed is moved. --discard drops the thumbnails
           instead of moving them (Capture One rebuilds what it opens). A
           CaptureOne folder without a Cache gets an empty one on the fast disk.
  move     --to NEW_ROOT: every junction found is pointed at NEW_ROOT, the
           cache moved along (--discard: not moved). A junction whose target
           is gone (the fast disk was replaced or wiped) gets an EMPTY folder
           at NEW_ROOT - `move --to <the same root>` is the repair.
  unlink   junction -> real Cache folder back next to the photos, the cache
           moved back (--discard: an empty folder, the copy on the fast disk
           is deleted).

Rules (c1_cache_rules.toml, see c1_cache_rules.example.toml): a name rule
(a folder name such as OUTTAKES) beats a path rule; the longest matching path
wins; otherwise the default root. "none" = beside the photos.

The junctions are the record of what is linked: every command finds them by
scanning the --source folders. The map (MAP_FILE_NAME in each cache root, plus
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
import tomllib
from dataclasses import dataclass, field

VERSION = "0.2.0"

# ===== Settings (edit here; the command-line flags override them) ==========
CACHE_ROOT = r"E:\C1_Cache"         # where the caches live when no rule says otherwise (--to)
RULES_FILE = "c1_cache_rules.toml"   # where caches go per folder; relative = next to this script (--rules)
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


# --- rules --------------------------------------------------------------------

RULE_KEYS = frozenset({"default", "path", "name"})   # top-level keys of the rules file


@dataclass
class Rules:
    default: str | None        # cache root; None = "none" (beside the photos)
    paths: list                # [(source folder, cache root or None)]
    names: list                # [(folder name, cache root or None)]


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _check_keys(table, allowed, path, where):
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {', '.join(unknown)} in {where} "
                         f"(allowed: {', '.join(sorted(allowed))})")


def _abs_folder(value, path, where):
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.splitdrive(value)[0]:
        raise ValueError(f"{path}: {where}: {value!r} is not an absolute folder with a drive "
                         "(like E:\\C1_Cache)")
    return os.path.abspath(value)


def _cache_root(value, path, where):
    """A cache root, or None for "none" (Capture One's own default)."""
    if isinstance(value, str) and value.casefold() == "none":
        return None
    return _abs_folder(value, path, where)


def _entries(data, key, path):
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(e, dict) for e in value):
        raise ValueError(f"{path}: '{key}' must be a list of [[{key}]] tables")
    return value


def _required(entry, key, path, where):
    if key not in entry:
        raise ValueError(f"{path}: {where}: missing '{key}'")
    return entry[key]


def load_rules(path, fallback_root):
    """Reads the rules file. fallback_root is the default when the file has none.
    Any problem raises ValueError with a message that names the file."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from None
    except OSError as exc:
        raise ValueError(f"{path}: cannot read: {exc}") from None
    _check_keys(data, RULE_KEYS, path, "the top level")
    default = fallback_root
    if "default" in data:
        default = _cache_root(data["default"], path, "default")
    paths, seen = [], {}
    for n, entry in enumerate(_entries(data, "path", path), 1):
        where = f"[[path]] number {n}"
        _check_keys(entry, {"source", "to"}, path, where)
        source = _abs_folder(_required(entry, "source", path, where), path, where + " source")
        to = _cache_root(_required(entry, "to", path, where), path, where + " to")
        if _key(source) in seen:
            raise ValueError(f"{path}: {where}: source {source} is already used by "
                             f"[[path]] number {seen[_key(source)]}")
        seen[_key(source)] = n
        paths.append((source, to))
    names, seen_names = [], {}
    for n, entry in enumerate(_entries(data, "name", path), 1):
        where = f"[[name]] number {n}"
        _check_keys(entry, {"name", "to"}, path, where)
        name = _required(entry, "name", path, where)
        if not isinstance(name, str) or not name.strip() or any(ch in name for ch in "\\/:"):
            raise ValueError(f"{path}: {where}: name {name!r} must be a folder name "
                             "(not empty, no \\ / :)")
        name = name.strip()
        if name.casefold() in seen_names:
            raise ValueError(f"{path}: {where}: name {name} is already used by "
                             f"[[name]] number {seen_names[name.casefold()]}")
        seen_names[name.casefold()] = n
        names.append((name, _cache_root(_required(entry, "to", path, where), path, where + " to")))
    return Rules(default, paths, names)


def _ancestor_names(c1_dir):
    """Folder names above the CaptureOne folder, outermost first (drive excluded)."""
    parent = os.path.dirname(os.path.abspath(c1_dir))
    return [part for part in os.path.splitdrive(parent)[1].split(os.sep) if part]


def _inside(folder, source):
    """True when folder is strictly below source (whole folder names, not text)."""
    prefix = _key(source)
    if not prefix.endswith(os.sep):
        prefix += os.sep
    return _key(folder).startswith(prefix)


def resolve(rules, c1_dir):
    """(cache root or None, label) for one CaptureOne folder. No disk access.
    A name rule beats every path rule; among name rules the one closest to the
    CaptureOne folder wins; among path rules the longest source wins."""
    names = {name.casefold(): (name, to) for name, to in rules.names}
    for folder in reversed(_ancestor_names(c1_dir)):
        if folder.casefold() in names:
            name, to = names[folder.casefold()]
            return to, f"name {name}"
    matches = [(src, to) for src, to in rules.paths if _inside(c1_dir, src)]
    if matches:
        src, to = max(matches, key=lambda rule: len(_key(rule[0])))
        return to, f"path {src}"
    return rules.default, "default"


def rule_roots(rules):
    """Every cache root the rules name (no duplicates, no 'none')."""
    found = [rules.default] + [to for _, to in rules.paths] + [to for _, to in rules.names]
    return list(dict.fromkeys(r for r in found if r))


# --- scan ---------------------------------------------------------------------

@dataclass
class Item:
    c1_dir: str
    cache: str
    state: str = ""            # real | linked | broken | none | odd
    target: str = ""
    nbytes: int = 0
    leftovers: list = field(default_factory=list)
    rule: str = ""             # which rule decided the cache root (for the map and the plan)

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
             "# state\tcache folder next to the photos\twhere it lives (junction target)\trule that chose it"]
    for item in sorted(items, key=lambda i: i.cache.casefold()):
        lines.append(f"{item.state}\t{item.cache}\t{item.target}\t{item.rule}")
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


# --- plan: which action each Capture One folder gets ----------------------------

def plan_item(item, rules):
    """(action, cache root or None) for one folder under the rules. The action is
    link | move | repair | unlink, or "" for nothing to do (in place or left alone)."""
    root, _ = resolve(rules, item.c1_dir)
    if item.state in ("real", "none"):
        return ("link", root) if root else ("", None)
    if item.state not in ("linked", "broken"):
        return "", root                          # odd: left alone, as before
    if root is None:
        return "unlink", None
    if not _same(item.target, mirror(root, item.cache)):
        return "move", root
    return ("repair" if item.state == "broken" else ""), root


def make_plan(items, rules):
    return [(item, *plan_item(item, rules)) for item in items]


def space_needs(todo, discard):
    """{drive: bytes that must fit there} for the planned (item, action, root) rows."""
    need = {}
    if discard:
        return need
    for item, action, root in todo:
        if action == "link" and item.state == "real":
            drive = _drive_key(root)
        elif action == "move" and item.state == "linked" and not _same(item.target, mirror(root, item.cache)):
            drive = _drive_key(root)
        elif action == "unlink" and item.state == "linked":
            drive = _drive_key(item.cache)
        else:
            continue
        need[drive] = need.get(drive, 0) + item.nbytes
    return need


def check_space(needs):
    """One Space line per drive; True when every drive has room for its share."""
    if not needs:
        print("Space: nothing to copy")
        return True
    margin = int(SPACE_MARGIN_GB * 1024 ** 3)
    ok = True
    for drive, need in sorted(needs.items()):
        free = free_bytes(drive + "\\")
        fits = need == 0 or free - margin >= need
        ok = ok and fits
        print(f"Space: {drive.upper()}  need {_gb(need)}, free {_gb(free)}, "
              f"margin {SPACE_MARGIN_GB:g} GB -> {'fits' if fits else 'DOES NOT FIT'}")
    return ok


def _dest_label(root):
    return "none (beside the photos)" if root is None else root


def print_plan(plan, rules):
    """Per destination: folders already in place and what would go there."""
    def new_row():
        return {"in place": 0, "link": 0, "move": 0, "repair": 0, "unlink": 0, "bytes": 0}
    rows = {dest: new_row() for dest in dict.fromkeys(
        [rules.default] + [to for _, to in rules.paths] + [to for _, to in rules.names])}
    for item, action, root in plan:
        if item.state == "odd":
            continue
        row = rows.setdefault(root, new_row())
        if action:
            row[action] += 1
            row["bytes"] += item.nbytes
        else:
            row["in place"] += 1
    print("PLAN: where each Capture One folder goes")
    print(f"  {'destination':<46s} {'in place':>8s} {'link':>6s} {'move':>6s} "
          f"{'repair':>6s} {'unlink':>6s} {'GB':>10s}")
    for dest, row in rows.items():
        print(f"  {_dest_label(dest):<46s} {row['in place']:8d} {row['link']:6d} {row['move']:6d} "
              f"{row['repair']:6d} {row['unlink']:6d} {_gb(row['bytes']):>10s}")


def _todo_line(item, action, root, with_rule):
    target = mirror(root, item.cache) if root else "beside the photos"
    tail = f"   [{item.rule}]" if with_rule else ""
    return f"  {(action or 'leftover'):8s} {_gb(item.nbytes):>10s}  {item.cache}  ->  {target}{tail}"


def apply_action(item, action, root, discard):
    if action == "link":
        return op_link(item, root, discard)
    if action in ("move", "repair"):
        return op_move(item, root, discard)
    if action == "unlink":
        return op_unlink(item, discard)
    return None


# --- rules file in use ---------------------------------------------------------

def _rules_path():
    if os.path.isabs(RULES_FILE):
        return RULES_FILE
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), RULES_FILE)


def _load_rules_for(args):
    """(Rules, path of the rules file or None). --to overrides the default root."""
    fallback = os.path.abspath(args.to or CACHE_ROOT)
    path = os.path.abspath(args.rules) if args.rules else _rules_path()
    if os.path.isfile(path):
        rules, rules_file = load_rules(path, fallback), path
    elif args.rules:
        raise ValueError(f"{path}: file not found (given with --rules)")
    else:
        rules, rules_file = Rules(fallback, [], []), None
    if args.to:
        rules.default = os.path.abspath(args.to)
    return rules, rules_file


def _print_rules(rules, rules_file):
    if rules_file:
        print(f"Rules:      {rules_file}: {len(rules.paths)} path rule(s), "
              f"{len(rules.names)} name rule(s)")
    else:
        print("Rules:      no rules file: every cache goes to the default root")
    print(f"Default:    {_dest_label(rules.default)}")


def _existing(roots):
    return [r for r in roots if os.path.isdir(r)]


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
                        help=f"cache root: overrides the default root (default {CACHE_ROOT}); "
                             "move needs it")
    parser.add_argument("--rules", metavar="PATH", default=None,
                        help=f"rules file (default: {RULES_FILE} next to this script)")
    parser.add_argument("--discard", action="store_true",
                        help="do not move the existing thumbnails; Capture One rebuilds them")
    parser.add_argument("--apply", action="store_true",
                        help="really change things (without it: a dry run)")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.to or CACHE_ROOT)
    if args.command == "move" and not args.to:
        print("move needs --to <new cache root> (the same root repairs gone targets)")
        return 2
    try:
        rules, rules_file = _load_rules_for(args)
    except ValueError as exc:
        print(f"Rules file error: {exc}")
        return 2
    sources = _sources(args)
    for src in sources:
        if not os.path.isdir(src):
            print(f"source not found: {src}")
            return 2
    print(f"c1_cache_link {VERSION} - {args.command}"
          f"{'' if args.apply or args.command == 'status' else '  (DRY RUN: nothing changes; add --apply)'}")
    print(f"Sources:    {'; '.join(sources)}")
    _print_rules(rules, rules_file)
    roots = rule_roots(rules)
    if args.command in ("link", "move"):
        for dest in roots:
            if any(_drive_key(s) == _drive_key(dest) for s in sources):
                print(f"  note: {dest} is on the same disk as the photos - no speed gain")
    if args.command == "move" and rules_file:
        print("  note: a rules file is in use: to change disks, edit default/to in it and run link; "
              "the next link will follow the rules, not this --to")

    t0 = time.monotonic()
    items, errors = scan(sources, skip_roots=roots)
    for item in items:
        item.rule = resolve(rules, item.c1_dir)[1]
    print(f"  scan: {len(items)} Capture One folders in {time.monotonic() - t0:.0f} s")
    _print_summary(items, errors)

    if args.command == "status":
        print_plan(make_plan(items, rules), rules)
        written = write_map(items, _existing(roots))
        for path in written:
            print(f"Map: {path}")
        return 1 if errors else 0

    plan = []
    if args.command == "link":
        plan = make_plan(items, rules)
        todo = [(item, action, dest) for item, action, dest in plan if action or item.leftovers]
    elif args.command == "move":
        todo = [(item, "move", root) for item in items if item.state in ("linked", "broken")]
    else:
        todo = [(item, "unlink", None) for item in items if item.state in ("linked", "broken")]

    needs = space_needs(todo, args.discard)
    total_need = sum(needs.values())
    print(f"To do: {len(todo)} Capture One folders, {_gb(total_need)} to move"
          f"{' (thumbnails discarded)' if args.discard else ''}")
    if not check_space(needs):
        print("Refused: not enough space. Use --discard (Capture One rebuilds the thumbnails it "
              "needs) or pick fewer --source folders.")
        return 2
    if not todo:
        print("Nothing to do.")
        if args.apply:
            write_map(items, _existing(roots))
        return 0
    if not args.apply:
        if args.command == "link":
            print_plan(plan, rules)
        for item, action, dest in todo[:20]:
            print(_todo_line(item, action, dest, with_rule=args.command == "link"))
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
    for n, (item, action, dest) in enumerate(todo, 1):
        try:
            had_leftovers = bool(item.leftovers)
            notes = recover_leftovers(item) if had_leftovers else []
            if had_leftovers and args.command == "link":
                action, dest = plan_item(item, rules)     # the state may have changed
            result = apply_action(item, action, dest, args.discard)
            done_bytes += item.nbytes
            elapsed = time.monotonic() - t0
            eta = ""
            if total_need and done_bytes and not args.discard:
                eta = f", ~{(total_need - done_bytes) * elapsed / done_bytes / 60:.0f} min left"
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
    for path in write_map(items, _existing(roots)):
        print(f"Map: {path}")
    print(f"Done in {(time.monotonic() - t0) / 60:.1f} min: {len(todo) - failures} ok, {failures} failed.")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
