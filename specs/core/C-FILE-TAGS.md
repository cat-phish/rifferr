# C-FILE-TAGS: Flexible attributes written as file tags
Status: Implemented
Milestone: M5

## Context
Architecture §1 and §8. beets keeps flexible attributes (`subgenres`, `releasedate`,
`date_added`, `albumkind`, …) only in its library database. `beet write` and `Item.try_write()`
write the fields `mediafile` knows, so other players (foobar2000, MusicBee, an iPod sync) never
see flexible attributes. The reference workflow (`docs/original-checklist.md`: DATE ADDED,
Sub Genre, RELEASEDATE) needs them in the files (author, 2026-11-23).

beets already has the general mechanism: a plugin can register extra media fields
(`BeetsPlugin.add_media_field(name, descriptor)`), after which every beets write path writes
them. Rifferr's beets plugin (`rifferr`, C-TEMPLATE-FUNCTIONS TEMPLATE-FUNCTIONS-2) registers the
fields a user maps in config. No new write path is added (non-negotiable 3): ChangeSet apply,
`beet write` and `beets.command` subprocesses all write through beets.

## Behavior
- FILE-TAGS-1: The `rifferr` beets plugin MUST read `rifferr.file_tags` from the beets config
  and register each entry as a media field through `BeetsPlugin.add_media_field`, so the
  in-process backend and `beet` subprocesses (TEMPLATE-FUNCTIONS-3) write the same tags.
- FILE-TAGS-2: An entry MUST map a beets field name to a file tag name and MAY set
  `multi: true`. The plugin MUST build storage styles per format from the tag name:
  Vorbis/FLAC/Opus comment `NAME`, ID3 `TXXX:NAME`, MP4 freeform `----:com.apple.iTunes:NAME`,
  APE `NAME`, ASF `NAME`. A per-format override MAY replace any of them.
- FILE-TAGS-3: `multi: true` MUST write a list field as one tag value per element (repeated
  Vorbis comments, multi-value TXXX, multiple MP4 values) and read it back as a list.
- FILE-TAGS-4: A name that is already a `mediafile` field or a beets fixed field, or a name
  registered twice, MUST fail worker start-up with a message naming the field.
- FILE-TAGS-5: Reading a file (import, `beet update`) MUST fill the mapped field from the tag,
  so a file tagged elsewhere round-trips.
- FILE-TAGS-6: Writing an empty or deleted field MUST remove the tag from the file.
- FILE-TAGS-7: Mapped fields MUST appear in the tag grid and review cards like any other field;
  the field list (`GET /api/profiles/{p}/fields`) marks them `file_tag: true`.
- FILE-TAGS-9: A `multi: true` entry MUST also give the field a list type in the library
  (`BeetsPlugin.item_types` / `album_types`, the type beets uses for `albumtypes`), so the
  value is stored, queried and coerced as a list, not as joined text. Without it a flexible
  attribute is a plain string.
- FILE-TAGS-10: Multi-valued tags MUST use each format's native multiple values: repeated
  Vorbis/FLAC comments, NUL-separated values in one ID3v2.4 frame, several MP4 values. Where
  the format has no multiple values (ID3v2.3, when beets' `id3v23` is on) the plugin MUST write
  one value joined with `rifferr.multi_separator` (default `"; "`), and read it back split the
  same way. Contract tests MUST pin what foobar2000-compatible readers (mutagen) see for each.
- FILE-TAGS-8: Contract tests MUST pin the round trip (write, re-read, delete) for FLAC, MP3 and
  M4A fixtures, single- and multi-valued.

## Interfaces
beets config (`<beetsdir>/config.yaml`):
```yaml
plugins: [rifferr]           # added by the worker when missing (TEMPLATE-FUNCTIONS-2)
rifferr:
  file_tags:
    subgenres: { tag: SUBGENRE, multi: true }
    releasedate: RELEASEDATE
    date_added: DATE ADDED
    albumkind: ALBUMKIND
    # per-format override:
    # catalog_note: { tag: CATNOTE, id3: "COMM:catalog" }
```
```python
class FileTagSpec(BaseModel, frozen=True):
    tag: str
    multi: bool = False
    vorbis: str | None = None; id3: str | None = None; mp4: str | None = None
    ape: str | None = None; asf: str | None = None
```

As implemented (2026-11-23; `rifferr.backends.beets.file_tags`, used by the `rifferr` beets
plugin in `templates.py`):
- beets logs and skips a plugin whose `__init__` raises, so `config.load` validates
  `rifferr.file_tags` before plugins load and refuses to start (FILE-TAGS-4). In a `beet`
  subprocess the same entries are already known good.
- FILE-TAGS-6 uses beets' `write` event: a mapped field that is absent, `""` or `[]` is set to
  `None` in its `tags`, which `MediaFile.update` deletes.
- FILE-TAGS-10: the ID3 list style joins values itself when beets' `id3v23` is on (mutagen
  would join them with `/`), and splits a v2.3 value at the separator's non-blank part (`;`),
  trimming spaces, so values tagged `Rock;Pop` in foobar2000 read as two.
- FILE-TAGS-7: there is no `GET /api/profiles/{p}/fields`; the library page response
  (`LibraryPage.file_tags`, from `LibraryBackend.file_tag_fields()`) lists the mapped fields,
  and the tag grid marks their column headers.
- `rifferr.multi_separator` (default `"; "`) must not be blank.
- An M4A fixture (`single-m4a`) was added to `scripts/make_fixtures.py`.

## Examples
- `releasedate: RELEASEDATE`, album `releasedate = "2004-03-22"`, ChangeSet applied →
  FLAC has `RELEASEDATE=2004-03-22`; MP3 has `TXXX:RELEASEDATE`.
- `subgenres: {tag: SUBGENRE, multi: true}`, value `["Post-Rock", "Math Rock"]` → two
  `SUBGENRE` comments; foobar2000 shows two values without splitting by `;`.

## Extensibility
Users add any field. The storage-style mapping per format is fixed in the plugin; overrides
cover odd cases.

## Security & safety
Config is beets config the user already controls; no code runs from it. Tag names are
validated as printable text without `=` (Vorbis) and NUL.

## Non-goals
- Writing fields beets already writes.
- Custom value encoding (dates stay text).

## Open questions
1. ~~ID3 multi-value?~~ Resolved 2026-11-23 (author): a player that understands multiple
   values sees them; one that doesn't sees one readable value (FILE-TAGS-10).
2. Should a file tag that exists in the file but is not mapped be preserved on write? (beets
   leaves unknown tags alone; to confirm in the contract test.)

## Tasks
- [x] Contract tests for FLAC/MP3/M4A round trip (FILE-TAGS-8)
- [x] `rifferr` beets plugin: read config, register fields, start-up validation
- [x] Field list marks file tags
- [x] docs/getting-started.md: config example
