# c1-cache-link — Capture One caches on a fast disk

Keeps Capture One's **`Cache` folders** (thumbnails `.cot`, previews `.cof`/`.cop`) on a fast SSD while the **edits stay next to the photos** on the big, slow disk. Each `...\CaptureOne\Cache` becomes an NTFS junction to the SSD; Capture One sees an ordinary folder and never knows. **Dry run by default, nothing deleted before its copy is checked, refuses while Capture One runs.** Rules decide which folders get the fast disk: by default all of them, but, for example, every `OUTTAKES` folder can keep its cache beside the photos.

**Current version: v0.2.0** (2026-11-26) · [What's new](#whats-new-in-v020) · Windows only · one Python file, no dependencies

---

## Why

Capture One writes a `CaptureOne` folder beside every folder of photos it opens: `Settings*` holds your edits, `Cache` holds thumbnails and previews it can always rebuild. On a hard disk, browsing a big folder means reading thousands of small cache files from the slowest disk in the PC. Moving the photos to an SSD is expensive; moving only the cache costs a fraction of the space. In my library, 435 folders on an 18 TB disk have about 190 GB of cache.

- **Browsing is fast** — the thumbnails and previews come from the SSD.
- **Edits stay with the photos** — `Settings*` never moves, so a backup of the photo disk still holds every adjustment, and moving or renaming a folder keeps its edits.
- **Nothing is lost if the SSD dies** — the cache is rebuilt by Capture One; one command recreates the junctions on a new disk.
- **The SSD does not have to hold everything** — rules send rarely opened folders (outtakes, old years) elsewhere, or leave them untouched.

---

## Features

- **Rules** — a default location, path rules (everything below `G:\2018` → another disk) and folder-name rules (`OUTTAKES` → beside the photos). One `link` reconciles every folder with the rules and moves only what differs.
- **A plan before you commit** — per destination: folders already in place, and how many would be linked, moved, repaired or brought back, with the GB.
- **A readable map** (`_c1_cache_map.tsv`) of every cache folder, where it lives and which rule chose it.
- **Hostile paths** — Japanese names, `[ ]`, spaces, `%`, paths past 260 characters, junctions across drives.

---

## Install

Windows 10/11, NTFS on both disks, **Python 3.11 or newer** (it reads the rules with the standard `tomllib`). Nothing else to install: download `c1_cache_link.py` and open the settings at its top:

```python
CACHE_ROOT = r"E:\C1_Cache"   # the fast disk (--to)
SOURCE_ROOTS = "G:\\"         # the photo disk(s) scanned, ';'-separated (--source)
```

Junctions need no administrator rights.

---

## Quick start

Close Capture One before any `--apply`.

```
python c1_cache_link.py status                  # every CaptureOne folder, where its cache lives, the plan
python c1_cache_link.py link                    # dry run: what would move where
python c1_cache_link.py link --apply            # do it: caches moved to the SSD, junctions created
python c1_cache_link.py link --apply --discard  # same, old thumbnails dropped (rebuilt on demand)
```

Run `link` again now and then: a folder Capture One opens for the first time gets a real `Cache` beside the photos.

Other commands:

```
python c1_cache_link.py move --to D:\C1_Cache   # no rules file: every junction repointed, caches moved along
python c1_cache_link.py move --to E:\C1_Cache   # the same root: a lost SSD comes back with empty caches
python c1_cache_link.py unlink --apply          # everything back beside the photos, junctions removed
```

`--source "G:\2025;G:\2026"` limits a run to some folders.

---

## Rules

Put them in `c1_cache_rules.toml` next to the script (or pass `--rules PATH`); start from [`c1_cache_rules.example.toml`](c1_cache_rules.example.toml). Without a rules file every cache goes to `CACHE_ROOT`.

```toml
default = 'E:\C1_Cache'        # every cache no rule matches ("none" = beside the photos)

[[path]]
source = 'G:\2018'             # everything below G:\2018 ...
to = 'F:\C1_Cache'             # ... goes to F:

[[name]]
name = "OUTTAKES"              # any folder called OUTTAKES, at any depth ...
to = "none"                    # ... keeps a real Cache beside its photos
```

- **Precedence:** a name rule beats every path rule (the name closest to the `CaptureOne` folder wins); among path rules the longest source wins, whatever the order in the file; otherwise `default`.
- **`"none"`** means Capture One's own default: a real `Cache` folder beside the photos, no junction.
- **Changing your mind:** edit the file, run `link` to see the plan, then `link --apply`. Only the caches whose place changed are moved; a cache a rule now sends to `"none"` comes back beside the photos.
- **New SSD:** change `default` (or a `to`) and run `link` — with a rules file, that replaces `move --to`.
- A malformed file (unknown key, relative path, duplicate source or name, bad TOML) is reported with its name, and nothing changes.

---

## Safety guarantees

One line each.

- **Nothing changes without `--apply`.** Every command first prints what it would do.
- **The edits are never touched.** Only a folder named `Cache` directly inside a `CaptureOne` folder is moved or deleted; `Settings*` and the photos stay where they are.
- **A cache is deleted only after its copy is complete and checked** (every file there, same size), or with `--discard` when you asked for it. A copy is made under a temporary name and renamed only when finished.
- **Fail closed.** Not enough space on any destination disk, Capture One running, a malformed rules file: the run stops before the first byte is copied, exit 2.
- **A failed folder is put back as it was**, and the run goes on with the next one; an interrupted run is cleaned up by the next one.
- **Nothing is skipped silently.** A `Cache` that is a file is counted in the summary and left alone; a folder that cannot be read is listed and makes the run exit 1.

---

## Good to know

- **Backups of the photo disk:** tell the backup tool not to follow junctions (in FreeFileSync, *Symbolic links: Exclude*), or it copies the cache from the SSD as if it lived there. Caches left beside the photos by a `"none"` rule are ordinary folders and are backed up; exclude `*\CaptureOne\Cache\` if you do not want them.
- **A lost or wiped SSD:** Capture One shows empty folders until the junctions point somewhere; `status` lists them as *link whose target is gone*, and `link` (or `move --to` the same root) recreates them empty. The edits were never on the SSD.
- **The junctions are the record**, not the map: every command finds them by scanning the photo disk, so it works even when the map is gone.
- **Layout on the SSD:** `<cache root>\<drive letter>\<same path as the photos>`, so a cache is easy to find by hand.
- **Interrupted run** (power cut, Ctrl+C): the next `link`/`move`/`unlink` puts a set-aside `Cache` back or deletes the leftover copy.
- **Same disk:** a cache root on the same disk as the photos is allowed but gains nothing; a note says so.

---

## What's new in v0.2.0

- **Rules** (`c1_cache_rules.toml`): a default, path rules and folder-name rules, each to a cache root or to `"none"` (beside the photos).
- **`link` reconciles** every folder with the rules: links new ones, moves those whose rule changed, repairs gone targets, brings back those sent to `"none"`.
- **Plan table** in `status` and in the `link` dry run; **free space checked per disk**; the map gains a `rule` column.
- Output never crashes on a name the console cannot print (Windows pipes).

v0.1.0 (2026-11-08): first release — one cache root, `status` / `link` / `move` / `unlink`.

---

## Tests

```
python -m pytest tests -q
```

47 tests, all in temporary folders: hostile names (Japanese, `[ ]`, spaces, `%`), a path past 260 characters, interrupted and failed copies rolled back, the refusal while Capture One runs and without space, a full cycle of rule changes with the cache files checked by hash, and a junction across drives when `E:\temp_jxl` exists.

---

## Related projects

All mine:

- [photo-backup](https://github.com/rsilvabr/photo-backup) — selective backup of a photo archive: the exported photos with their originals and their Capture One edits. It skips Capture One's `Cache` folders by default, so the two work together: the edits are backed up, the cache is not.
- [jxl-photo](https://github.com/rsilvabr/jxl-photo) — TIFF and JPEG to JPEG XL and back, for photographers: JXL masters with the ICC profile and metadata.
- [tiff-workflow](https://github.com/rsilvabr/tiff-workflow) — lossless TIFF recompression, no libjxl needed.

---

## Disclaimer

This tool was made for my personal workflow.
Use at your own risk — I am not responsible for any issues you may encounter.

However, if you find any bugs, feel free to report them to me — I will gladly try my best to improve this project.

Always start with `status` and a dry run, and try one `--source` folder before the whole disk.

---

## License

MIT License — feel free to use, modify, and distribute.

---

## Acknowledgments

- [Claude](https://www.anthropic.com/claude) (Anthropic), among other AI tools, for code assistance, reviews and technical discussion
