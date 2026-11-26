# C-FOLDER-FACTS: Album facts from the files in an album's folder
Status: Draft
Milestone: M5

## Context
Architecture §4 (tag scripts) and C-ALBUM-FACTS. Checklist line 12 (`docs/coverage.md`)
asks for Singles, EPs, Anthologies, Compilations, 24-bit and Vinyl "in album names and tags".
Singles, EPs and Compilations are done (rows 6.1, 6.3). 24-bit goes in folder names only,
never tags (B-046, author 2026-11-13), and the path formats already do that (row 15). Two
gaps remain:

- **Vinyl** (B-045): every vinyl release goes to review, decided from the files in the album
  folder that show a vinyl rip (rip logs), not from beets' `media`, which often says Vinyl
  when the files aren't from vinyl (author, 2026-11-13). No step can see an album folder's
  non-audio files today.
- **Anthology** (row 6.3): today the compilation review is approved or rejected, and an
  Anthology is made by rejecting and editing in the tag grid. `tags.choose` (S-TAGS-CHOOSE)
  can now offer Compilation and Anthology as two options.

This spec adds one general capability, facts about the files beside the audio, configured
per profile like ALBUM-FACTS-2 item counts. Nothing about vinyl or rip logs is built in.

## Behavior
- FOLDER-FACTS-1: A profile MAY name folder facts in the beets config:
  `rifferr.folder_facts: {<name>: {glob: <pattern>, contains: <regex>?}}`. Each becomes
  `rf_<name>`, an integer: the number of files in the album's folder whose name matches `glob`
  (case-insensitive, `fnmatch` syntax) and, when `contains` is given, whose text matches the
  regex (case-insensitive, Python `re`).
- FOLDER-FACTS-2: The album's folder is the deepest folder that contains every item of the
  album. Its subfolders are searched too (multi-disc rips keep logs in `CD1/`), at most 2
  levels below it. Audio files of the album's items are never counted.
- FOLDER-FACTS-3: Only regular files are read. A symlink, or a path that resolves outside the
  album's folder, MUST be skipped. At most the first 256 KiB of a file are matched against
  `contains`, decoded as UTF-8, then UTF-16 when it starts with a BOM, then Latin-1 (rip logs
  are often UTF-16). A file that can't be read counts as not matching and adds a note to the
  run log; it never fails the album.
- FOLDER-FACTS-4: Folder facts behave as other album facts: typed integers usable in `when`
  queries (`rf_vinyl_log:1..`) and in templates (`$rf_vinyl_log`), read-only
  (ALBUM-FACTS-5), and computed by FakeBackend from a fixture's `files` list
  (ALBUM-FACTS-6). Unlike item facts they come from disk, not from working values: they are
  read once per album per step and cached for that step.
- FOLDER-FACTS-5: A folder fact name MUST NOT collide with a built-in fact or an
  ALBUM-FACTS-2 item count; a collision fails worker startup with a message naming both.
- FOLDER-FACTS-6: An album whose items have no path (not yet on disk) MUST get `0` for every
  folder fact.
- FOLDER-FACTS-7: `foobar-style` MUST send every album whose folder facts or `media` suggest
  vinyl to a `tags.choose` review (`vinyl`), suggesting Vinyl when the rip-log fact is above
  zero and Not vinyl otherwise. Its options are set in the example workflow, not here (see
  Open questions 1–3).
- FOLDER-FACTS-8: `foobar-style`'s compilation step MUST become a `tags.choose` review with
  Compilation and Anthology options and a custom entry, replacing approve/reject
  (`examples/scripts/compilation.yaml` keeps working for other workflows).

## Interfaces
beets config (the profile's):
```yaml
rifferr:
  folder_facts:
    vinyl_log: { glob: "*.log", contains: "vinyl|turntable|phono|needle drop" }
    rip_notes: { glob: "*.txt", contains: "vinyl" }
```
```python
class FolderFact(BaseModel):
    glob: str = Field(min_length=1)
    contains: str | None = None   # regex, checked when the config loads
```
FakeBackend fixture:
```yaml
albums:
  - album: X
    files: [{ name: "rip.log", text: "EAC ... Technics turntable" }]
    items: [...]
```

## Examples
| folder contents | `vinyl_log` fact (config above) |
|---|---|
| `01.flac`, `rip.log` ("... turntable ...") | 1 |
| `01.flac`, `rip.log` (EAC CD log) | 0 |
| `CD1/01.flac`, `CD1/vinyl.LOG` ("Vinyl") | 1 |
| `01.flac`, `notes.log` → symlink to `/etc/passwd` | 0 (skipped) |

`foobar-style` (sketch, options pending the open questions):
```yaml
- id: vinyl
  uses: tags.choose
  with:
    query: "rf_vinyl_log:1.. , rf_rip_notes:1.. , media:vinyl"
    show: [album, media, rf_vinyl_log]
    options:
      - label: Vinyl
        suggest: { when: "rf_vinyl_log:1.. , rf_rip_notes:1.." }
        actions: [...]
      - label: Not vinyl
        actions: [...]
```

## Extensibility
Any number of folder facts per profile; any workflow can use them. The search depth (2) and
read limit (256 KiB) are fixed.

## Security & safety
Folder facts read files the user put next to their music, inside folders Rifferr already
imports. They never follow symlinks or leave the album's folder (FOLDER-FACTS-3), never read
more than 256 KiB per file, and never write. File contents never reach logs; only the file
name and the error appear in a read-failure note. Regexes come from the user's own beets config;
a pathological regex is bounded by the read limit.

## Non-goals
- Facts from audio analysis (spectrograms, noise floor) to detect vinyl.
- Reading cue sheets or log formats structurally (EAC/XLD checksums).
- Folder facts as strings (file names or matched text); counts only, until a script needs more.

## Open questions
1. What does Vinyl change? Proposed: album name suffix " (Vinyl)" (guarded like the EP
   suffix), `media: Vinyl`, and the folder name? Which of these, and in what format?
2. What does Not vinyl change, when beets said Vinyl: set `media` to `Digital Media`, clear it,
   or leave it?
3. Which files and words show a vinyl rip in your downloads (`*.log` with "vinyl"? `*.txt`
   rip notes? a `vinyl` folder name?). The example config above is a guess.
4. Anthology: same suffix style as Compilation (" (Anthology)"), and does it set
   `albumtype`/`albumkind` too? **Answered (author, 2026-11-25):** album-kind offers
   Compilation (various artists, beets `comp`) and Anthology (one artist's compilation),
   each setting its suffix and `albumkind`; anthologies stay under their album artist.
5. Should 24-bit stay folder-only (B-046)? If yes, coverage row 12 counts 24-bit as done.

## Tasks
- [ ] Folder facts config, loading and collision check (beets backend)
- [ ] Folder scan with the safety rules; FakeBackend `files` fixtures
- [ ] Tests per behavior, beets contract test with a temp folder
- [ ] foobar-style `vinyl` and compilation/anthology choices; coverage row 12; B-045 done
