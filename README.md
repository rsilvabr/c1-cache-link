# c1-cache-link

Keeps Capture One's `Cache` folders (thumbnails `.cot`, previews `.cof`/`.cop`)
on a fast disk while the edits (`CaptureOne\Settings*`, `.cos`/`.comask`) stay
next to the photos. Each `...\CaptureOne\Cache` becomes an NTFS junction to
`<cache root>\<drive>\<same path>`. Capture One sees an ordinary folder.

Nothing changes without `--apply`; every run first lists what it would do.
Close Capture One before an `--apply` (the script refuses while it runs).

```
python c1_cache_link.py status                          # what is linked where; writes the map
python c1_cache_link.py link                            # G:\ caches -> E:\C1_Cache (moved)
python c1_cache_link.py link --discard                  # same, old thumbnails dropped (rebuilt on demand)
python c1_cache_link.py move --to D:\C1_Cache           # new fast disk: caches moved, junctions repointed
python c1_cache_link.py move --to D:\C1_Cache --discard # same without moving the thumbnails
python c1_cache_link.py move --to C:\C1_Cache           # repair: targets that are gone come back empty
python c1_cache_link.py unlink                          # caches back next to the photos
python c1_cache_link.py unlink --discard                # junctions removed, empty Cache folders back
```

`--source "G:\2025;G:\2026"` limits a run to some folders. Defaults
(`CACHE_ROOT`, `RULES_FILE`, `SOURCE_ROOTS`, the margin, the folder names) are
the settings at the top of the script.

## Rules

Each Capture One folder can send its cache to a different place. Put the rules
in `c1_cache_rules.toml` next to the script (or pass `--rules PATH`); start from
`c1_cache_rules.example.toml`:

```toml
default = 'E:\C1_Cache'        # where every cache goes unless a rule says otherwise ("none" = beside the photos)

[[path]]
source = 'G:\2018'             # everything below G:\2018 ...
to = 'F:\C1_Cache'             # ... goes to F:

[[name]]
name = "OUTTAKES"              # any folder called OUTTAKES, wherever it is ...
to = "none"                    # ... keeps a real Cache beside its photos
```

- **Precedence**: a name rule beats every path rule; among path rules the
  longest matching source wins (the order in the file does not matter);
  otherwise the default. Among name rules the one closest to the `CaptureOne`
  folder wins.
- **"none"** (any case) means Capture One's own default: a real `Cache` folder
  beside the photos, no junction.
- **No rules file**: every cache goes to `CACHE_ROOT` (or `--to`), as before.
  With a rules file, `--to` overrides its `default`.
- **Changing rules**: edit the file and run `link`. `link` moves only the caches
  whose target differs from the rules (a cache a rule now sends to "none" comes
  back beside the photos). `move --to` ignores the rules; a note says so.
- **Dry run first**: `link` (without `--apply`) prints a PLAN table: per
  destination, how many folders are already in place and how many would be
  linked, moved, repaired or unlinked, with the GB. `status` prints the same table.
- **Space** is checked per drive: each drive needs its share plus
  `SPACE_MARGIN_GB`.
- A malformed rules file (unknown key, relative path, duplicate source or name,
  bad TOML) is reported with its file name, and nothing changes.

- **Space**: moving refuses before the first byte when the destination lacks
  the cache size plus `SPACE_MARGIN_GB`. `--discard` needs no space.
- **The map** (`_c1_cache_map.tsv` in the cache root, plus `~/.c1_cache_map.tsv`)
  lists every cache folder, its state and where it lives. The junctions
  themselves are the working record: `move` finds them by scanning, so a lost
  fast disk is repaired from the photo disk alone.
- **Interrupted run**: the next `link`/`move`/`unlink` puts a set-aside Cache
  back or deletes the leftover (it only ever holds cache).
- **New folders**: Capture One creates a real `Cache` in a folder it opens for
  the first time; run `link` again now and then.
- **Backups of the photo disk**: tell the backup tool not to follow junctions,
  or it copies the cache from the fast disk as if it lived there.
- Only a folder named `Cache` directly inside a `CaptureOne` folder is ever
  deleted, and only after its contents were copied and checked (or with
  `--discard`).

Tests: `python -m pytest tests -q` (hostile paths: Japanese, `[ ]`, spaces,
`%`, a path past 260 characters, a junction across drives when `E:\temp_jxl`
exists).
