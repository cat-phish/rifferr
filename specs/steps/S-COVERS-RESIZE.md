# S-COVERS-RESIZE: `covers.resize` step
Status: Implemented
Milestone: M3

## Context
Covers from providers (S-COVERS-SEARCH) and from beets' fetchart are written as downloaded,
often 3000px or more. Many players and devices want a smaller `cover.jpg`, and the author
wants to keep the full-size original next to it (author, 2026-11-22: "resize any cover art
larger than 1200x1200 down to that size as cover.jpg, and leave the original as
cover-hires.jpg"). This is B-061. Architecture §6 (covers), §5 (steps), §8 (backend protocol).

beets' fetchart `maxwidth` resizes in place and keeps no original, and it only runs on
covers that fetchart fetched itself. So it can't do this. beets' `ArtResizer` can be reused
for the resize itself, but only inside `rifferr.backends.beets`. This step goes through the
backend protocol instead, so its resize uses Pillow directly, behind an injected interface.

## Behavior
- COVERS-RESIZE-1: For each album of the run whose art (`art_sha256`) is larger than `max`
  pixels in width or height, the step MUST propose one ChangeSet. The ChangeSet holds
  (a) a `files` FileEffect (kind `artifact`, so the library's art path isn't changed) that
  copies the original image from the art store to the name given by `original`, in the
  art's folder, and (b) an `art_sha256` field change to the resized
  image, which is stored in the profile's art store first. The resized image MUST keep the
  aspect ratio and have its longest side equal to `max`.
- COVERS-RESIZE-2: Albums without art, and albums whose art is at most `max` on both sides,
  MUST be skipped with no ChangeSet. Dimensions are read with `core.covers` `ImageInfo`.
- COVERS-RESIZE-3: The step MUST be idempotent. On a second run the art is at most `max`,
  so nothing is proposed, and the kept original is never overwritten.
- COVERS-RESIZE-4: If a file named `original` already exists in the folder and its content
  differs from the current art, the album MUST fail with a clear error and no change (a file
  is never overwritten, as in LIBRARY-MOVE). If it is byte-identical, the copy is skipped and
  only the art changes.
- COVERS-RESIZE-5: Undo (C-JOURNAL) MUST put the original art back and remove the copy it
  made. The copy's source is the original in the art store, so undo can check it. If the
  copy was changed afterwards, undo refuses and names it (LIBRARY-MOVE-15), and the file
  stays.
- COVERS-RESIZE-6: `apply` MUST default to `auto`. With `review`, the review shows the old and
  new size and the kept file's name.
- COVERS-RESIZE-7: The output format follows `format`: `keep` writes the art's own format
  (JPEG stays JPEG, PNG stays PNG); `jpeg` always writes JPEG at `quality`. The art file's
  name follows from the backend's art path, which beets' `art_filename` decides. This step
  never names it.
- COVERS-RESIZE-8: The step MUST need the backend capabilities `query`, `apply`, `art` and
  `move`, and fail validation when they are missing. It MUST be registered through the
  public `rifferr.steps` entry point, and the Pillow resizer through `rifferr.image_resizers`.
- COVERS-RESIZE-9: When the album's snapshot has `art_kept: true` (its cover is an image it
  came with, which stays in place when the cover changes, COVERS-SEARCH-16), the step MUST
  NOT copy the original: that file already is the kept original. Only `art_sha256` changes,
  and the rationale names the file that stays (B-096).

## Interfaces
```yaml
- id: art-size
  uses: covers.resize
  with:
    max: 1200                       # px, longest side; required, >= 1
    original: "{stem}-hires{ext}"   # the kept original's name; {stem} {ext} of the art file
    format: keep                    # keep | jpeg
    quality: 90                     # JPEG quality 1–95
    resizer: pillow                 # a rifferr.image_resizers entry point
```
- `rifferr.covers.resize.ImageResizer` protocol: `name`, `resize(data: bytes, max_side: int,
  fmt, quality) -> bytes`, raising `ResizeError`. The built-in `PillowResizer` uses `Image.thumbnail(..., LANCZOS)` and
  keeps ICC profiles. The step gets it injected; tests use a fake.
- New dependency: `pillow` (HPND license, AGPL-compatible).

## Examples
A 3000x3000 `cover.jpg` with `max: 1200`: `cover-hires.jpg` is a byte copy of the original,
and `cover.jpg` is 1200x1200. A 1600x900 PNG with `format: keep`: 1200x675 PNG. A 1000x1000
cover: no ChangeSet.

## Extensibility
Plugins can register another `ImageResizer` (for example ImageMagick) through the
`rifferr.image_resizers` entry-point group and choose it with `resizer:` (default `pillow`).
A FileChangeSet can carry field changes to its album through `StepContext.propose_files(...,
extras=FileExtras(changes, rationale))`, applied after its files; this step uses that public
API. The ChangeSet shape is
fixed.

## Security & safety
Images are decoded with Pillow's `MAX_IMAGE_PIXELS` limit kept on (decompression bombs fail
the album). The step reads and writes only the art store and the album's folder, through
the backend.

## Non-goals
- Embedding art into audio files (B-060).
- Resizing non-cover images in the album folder.

## Open questions
1. Should `max` allow separate width and height limits? (Proposed: no; one longest-side limit.)

## Tasks
- [x] Spec approved (author, 2026-11-22)
- [x] `pillow` dependency; `ImageResizer` + `PillowResizer`; entry point
- [x] `covers.resize` step, params model, registry; schemas regenerated
- [x] Engine/journal: art change plus kept-file copy, undo (COVERS-RESIZE-1, -5)
- [x] Tests per behavior; idempotence property test; beets-backend temp-profile test
- [x] Fixture images generated in the tests with Pillow (no files in the repo)
- [x] `foobar-style.yaml`: `art-size` after `art`; keep `*-hires.*` in the album folder on move; `docs/coverage.md`
- [x] Close B-061
