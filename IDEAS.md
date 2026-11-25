# Idea: route each Capture One cache to a disk by rules

Status: implemented in 0.2.0 (rules file `c1_cache_rules.toml`, see README
"Rules"). Written 2026-11-25 by the owner's request, to be discussed with Opus
once the AI credit renews. The owner wants
Opus to understand both the proposal and the WHY, then design the
implementation.

## Today

One cache root for everything: `CACHE_ROOT` (or `--to`). `link` points every
`...\CaptureOne\Cache` at `<root>\<same path>` with an NTFS junction. Currently
`C:\C1_Cache`; the owner will move to an E: SSD of 512 GB later.

## The proposal (three layers)

1. **Default location.** Say where caches go when no rule matches. Either one
   disk/path (as now), or "Capture One's own default": do not touch the folder
   at all (no junction, the real `Cache` stays beside the photos).

2. **Path rules.** A list of source paths; everything below one (recursively)
   gets its cache on the disk named for that rule. Example: `G:\2025\...` ->
   fast SSD, `G:\2018\...` -> slow SSD. The first rule that matches wins (the
   most specific path should win; Opus to decide).

3. **Name rules.** A list of subfolder NAMES (e.g. `OUTTAKES`) that, wherever
   they appear under a source path, send their cache somewhere else again
   (another disk, or the default / untouched). A name rule beats the path rule
   of the folder that contains it.

## Why (the owner's workflow)

- **Fast SSD (E:)** holds the caches of what the owner is working on: folders
  not yet selected/edited, and folders already edited, but NOT the `OUTTAKES`
  folders.
- **OUTTAKES** are rarely opened, so their cache can stay on the old disk (or
  beside the photos, untouched). This is why name filtering is needed: a path
  rule alone cannot say "everything except OUTTAKES".
- **The SSD can fill up.** A 512 GB SSD will not hold the caches of the whole
  library (193 GB for the G: library in Nov 2026 and growing). Path rules let
  the owner put old years on a slow SSD and new years on the fast one, and move
  a year to another disk when the fast one is getting full.

## Points for the design discussion

- Where the rules live: a config file next to the script (JSON/TOML/TSV?),
  never hardcoded in the script. The existing map file
  (`_c1_cache_map.tsv`, `~/.c1_cache_map.tsv`) would need the target per
  folder; keep it readable.
- Interaction with the commands: `status` should show which rule each folder
  matches and whether its junction already points there; `link` / `move`
  should only touch the folders whose target differs from what the rules say
  (a re-run after a rule change moves just those caches). `unlink` stays as is.
- "Default C1" as a target means a folder with a rule-to-nothing must be
  UNLINKED (junction -> real folder, cache moved back), not left alone.
- Free-space checks per target disk (the script already refuses when space is
  missing, but now per disk), and `--discard` per rule?
- Rule order/precedence, and a dry-run report listing, per rule, how many
  folders and GB would go where (the owner decides by those numbers).
- Moving a cache between two junction targets must be safe if interrupted
  (the same fail-closed rule as today; Capture One closed).
- Tests: hostile paths as always (Japanese, `[ ]`, spaces) for both the rules
  and the targets; a name rule at several depths; two rules matching one folder.
