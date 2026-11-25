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
(`CACHE_ROOT`, `SOURCE_ROOTS`, the margin, the folder names) are the settings
at the top of the script.

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
