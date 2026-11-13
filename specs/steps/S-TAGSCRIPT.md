# S-TAGSCRIPT: `tags.script` step, action set and live preview
Status: Implemented
Milestone: M1

## Context
`tags.script` gives foobar2000 Masstagger's power with less ceremony
(architecture §4): an ordered list of actions, each with an optional `when`
(beets query) and a scope, evaluated with beets template syntax. The step only
proposes: its result is ChangeSets (C-CHANGESET) that follow the step's `apply`
policy. The M1 done-when (architecture §11) needs a script with live preview and
an idempotence check whose ChangeSets can be reviewed, applied and undone.

The evaluator lives in `rifferr.tagscript` and is pure (AGENTS.md layout): it
never imports beets. Templates, queries and value types are the backend's job,
reached through `LibraryBackend` (C-BACKEND amendments below).

Author decisions (2026-11-11):
- actions run in order, and each one sees the results of the earlier ones;
- a run checks idempotence and warns by default (`idempotence: warn | fail | off`);
- preview runs in a separate read-only worker per profile, so it stays live while
  a run is importing;
- the reference "release-type suffix" script waits for the author's checklist
  (Open questions); the examples here are illustrative.

## Behavior
### Scripts and actions
- TAGSCRIPT-1: The step MUST take either inline `actions` or `script: <path>`, not both. `script` is a path relative to the profile's `scripts/` folder; an absolute path, `..` or a symlink leaving that folder MUST be a validation error. A script file holds `{scope?, idempotence?, actions}` (published schema `schemas/tagscript.schema.json`); step parameters override the file's `scope` and `idempotence`. `{{ }}` interpolation (C-WORKFLOW) applies to the script file's text as it does to inline actions, once at step start.
- TAGSCRIPT-2: Each action MUST be a mapping with exactly one action key (`set`, `copy`, `move`, `replace`, `split`, `join`, `case`, `from_filename`, `delete`, `append_unique`) plus optional `when` (a backend query), `scope` (`album` | `item`, default the script's `scope`, which defaults to `album`) and `name` (a label for logs and preview). Unknown keys, several action keys or an empty action MUST be validation errors with the action's YAML line and column.
- TAGSCRIPT-3: Actions MUST run in order. Each action reads the working values left by the earlier actions, including in its `when` and its templates. An album-scoped action that changes a field the backend shares between an album and its items (`LibraryBackend.album_item_fields()`) MUST update that field in the working values of the album's items too, as beets does when storing.
- TAGSCRIPT-4: In an album-scoped script, an item-scoped action MUST run once per item of each selected album, in track order. In an item-scoped script, album-scoped actions MUST be a validation error.
- TAGSCRIPT-5: `when` MUST be evaluated with `backend.matches` on the target's working values. A target that doesn't match is left unchanged by that action.

### Action semantics
- TAGSCRIPT-6: `set: {field: value, …}`: a string value MUST be evaluated as a beets template on the working values (`backend.evaluate`), then converted to the field's type with `backend.coerce(field, text)`. A YAML number or boolean is used as is (after `coerce`); a list of strings sets a multi-value field, each element templated. Fields are set in the order written, each seeing the previous.
- TAGSCRIPT-7: `copy: {from, to}` MUST copy the value unchanged (no template, same type, converted with `coerce` only if the types differ). `move` is `copy` followed by `delete` of `from`. An absent `from` changes nothing.
- TAGSCRIPT-8: `replace: {fields: [...], find: <text>, with: <text>}` replaces literal text; `replace: {fields, regex: <pattern>, with: <text>, flags?: [i, m, s]}` uses Python `re` (`\1` / `\g<name>` in `with`). Exactly one of `find` / `regex` is allowed; an invalid regex is a validation error. On a multi-value field it MUST apply to each element. Non-string values are left unchanged.
- TAGSCRIPT-9: `split: {field, by, into?, strip: true}` MUST split a string into a multi-value field (`into` defaults to `field`), dropping empty parts and, with `strip`, surrounding whitespace. On a value that is already a list it MUST split each element and flatten, so it is idempotent. `join: {field, with, into?}` MUST join a list into a string; a string value is left as is. The backend's `check` rejects a list for a single-value field (CHANGESET-2).
- TAGSCRIPT-10: `case: {fields: [...], style: lower | upper | title | sentence, keep?: [words]}` MUST change letter case. `title` capitalizes the first letter of each word and `sentence` only the first letter of the value; neither lowercases the rest of a word. Words in `keep` (matched case-insensitively, written as given) are left in the given form. No word list is built in: language and style conventions come from the script or a rules file.
- TAGSCRIPT-11: `from_filename: {pattern, source?: stem | name | path}` is item-scoped only. It MUST match the item's file stem (default) against a pattern of literal text and `$field` placeholders, and set each placeholder's field from the matched text (through `coerce`). A filename that doesn't match changes nothing and is reported in preview.
- TAGSCRIPT-12: `delete: [fields]` MUST remove the fields (op `delete`). Deleting an absent field changes nothing. Fields the backend can't delete (beets fixed fields) are rejected by `check`; clear those with `set: {field: ''}`.
- TAGSCRIPT-13: `append_unique: {field, values: [...]}` MUST add each templated value to a multi-value field unless an equal element is already present, keeping order.

### Result and run
- TAGSCRIPT-14: After the last action, the step MUST compare each target's working values with the snapshot it started from and propose only the net differences (a field set and then restored is no change), with `old` from the snapshot, through `ctx.propose` (one ChangeSet per album, CHANGESET-14). Each field change MUST carry the names or indexes of the actions that changed it, as its `rationale`.
- TAGSCRIPT-15: A template, query or `coerce` error MUST fail that target (handled by `on_error`, RUN-5), naming the action (index and `name`), field and target, and nothing is proposed for that album.
- TAGSCRIPT-16: Unless `idempotence: off`, the step MUST run the script a second time on the first pass's result. If the second pass would change anything, the step MUST report, per field, the action responsible, the first-pass value and the second-pass value. With `warn` (default) it logs a warning and still proposes the first pass's changes; with `fail` the album's target fails and nothing is proposed for it.
- TAGSCRIPT-17: The step MUST log one summary line per album: the number of field changes proposed or "no changes", and the idempotence result. It MUST record the script text it ran (and the script file's path and hash) in its step record, so review shows what produced a ChangeSet.
- TAGSCRIPT-18: The step needs no trust: it can only propose (C-CHANGESET), and does not run user code. The static safety report MUST still list `tags.script` steps with `apply: auto` as an informational finding.

### Idempotence of the built-in actions
- TAGSCRIPT-19: `copy`, `move`, `split`, `join`, `case`, `from_filename`, `delete` and `append_unique` MUST be idempotent for every input (Hypothesis property tests). `set` and `replace` can be idempotent or not depending on their parameters; for them the property is that the TAGSCRIPT-16 check reports a difference exactly when a second pass changes something.

### Preview
- TAGSCRIPT-20: `POST /api/profiles/{profile}/tagscript/preview` MUST evaluate a script (inline YAML text or a script file path) against a backend query, without proposing or writing anything. It returns, per target: label, before and after values of the changed fields, which actions hit it, errors, and the idempotence result with the responsible action. It takes `limit` (default 50 albums, max 500) and reports `truncated`. Validation errors come back with YAML line and column. No events are persisted.
- TAGSCRIPT-21: Preview MUST run in the profile's read-only worker (F-WORKER amendment), so it answers while a run is active. A preview request MUST time out after 10 seconds with `504 preview_timeout`. A newer preview from the same client MUST supersede an older one still running (the older one is cancelled and answers `409 superseded`).
- TAGSCRIPT-22: The script editor page (`/scripts`, `/scripts/:name`) MUST list the profile's script files, edit one as YAML text, and show the live preview grid (before/after per target and field, changed cells highlighted, actions hit) for a query the user enters, updating 300 ms after the last edit. A second-pass difference MUST highlight the responsible action and the cells it affects. Saving MUST send the hash of the text that was loaded and get `409 hash_mismatch` if the file changed in between (API addition below). Text the editor saves is written as typed, so comments and ordering survive.

### C-BACKEND amendments this spec needs
- BACKEND-10 (new): `evaluate` and `matches` MUST use the field values in the snapshot they are given, not the stored ones, so they work on working values. Fields absent from the snapshot fall back to the stored object.
- BACKEND-11 (new): `coerce(field, value) -> FieldValue` MUST convert a value to the field's type (beets: the field's type `parse`; flexible attributes stay strings) and raise `ValueError` with a message when it can't.
- BACKEND-12 (new): `album_item_fields() -> frozenset[str]` MUST return the album fields the backend copies to items on store (beets: `Album.item_keys`). `FakeBackend` returns its configured set (CHANGESET-12).
- BACKEND-13 (new): A backend MUST be openable read-only (`from_profile(…, read_only=True)`). Read-only, it reports `apply` and `import_` as unsupported, and `BeetsBackend` sets `PRAGMA query_only` on its connection, so no write can happen. A contract test pins that writes fail.

### F-WORKER and F-API amendments this spec needs
- WORKER-11 (new): The server MAY start a second worker per profile in read-only mode (`-m rifferr.worker --profile P --read-only`) for preview and query jobs. It opens the backend read-only (BACKEND-13), accepts no run, apply or undo jobs, takes requests over the same IPC channel (WORKER-9), and follows the same idle timeout, heartbeat and shutdown rules. Its jobs are not stored in the job table: they are short-lived and lost on a crash, and the request fails with `503 preview_unavailable`.
- API-18 (new): `GET /api/profiles/{profile}/scripts` lists script files (name, hash, valid, errors); `GET …/scripts/{name}` returns text and hash; `PUT …/scripts/{name}` `{text, sha256?}` saves, with `409 hash_mismatch` if `sha256` is given and differs from the file on disk. Names follow the workflow stem rules (API-3) and stay inside `scripts/`.

## Interfaces
```python
ActionScope = Literal["album", "item"]

class ActionBase(BaseModel, frozen=True):
    when: str | None = None
    scope: ActionScope | None = None    # default: script scope
    name: str | None = None

class SetAction(ActionBase):    set: dict[str, str | int | float | bool | list[str]]
class CopyAction(ActionBase):   copy: CopySpec          # from, to
class MoveAction(ActionBase):   move: CopySpec
class ReplaceAction(ActionBase): replace: ReplaceSpec   # fields, find | regex, with, flags
class SplitAction(ActionBase):  split: SplitSpec        # field, by, into, strip
class JoinAction(ActionBase):   join: JoinSpec          # field, with, into
class CaseAction(ActionBase):   case: CaseSpec          # fields, style, keep
class FromFilenameAction(ActionBase): from_filename: FromFilenameSpec  # pattern, source
class DeleteAction(ActionBase): delete: list[str]
class AppendUniqueAction(ActionBase): append_unique: AppendSpec   # field, values

Action = SetAction | CopyAction | ... | AppendUniqueAction   # discriminated by key

class TagScript(BaseModel, frozen=True):           # script file / inline
    scope: ActionScope = "album"
    idempotence: Literal["warn", "fail", "off"] = "warn"
    actions: list[Action]                          # min 1

class TagScriptParams(BaseModel):                  # step `with`
    actions: list[Action] | None = None
    script: str | None = None                      # relative to <profile>/scripts/
    scope: ActionScope | None = None
    idempotence: Literal["warn", "fail", "off"] | None = None

# rifferr.tagscript (pure):
class Evaluator(Protocol):        # implemented over LibraryBackend
    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str: ...
    def matches(self, query: str, target: ItemSnapshot | AlbumSnapshot) -> bool: ...
    def coerce(self, field: str, value: FieldValue) -> FieldValue: ...
    def album_item_fields(self) -> frozenset[str]: ...

def run_script(script: TagScript, album: AlbumSnapshot, ev: Evaluator) -> ScriptResult: ...
# ScriptResult: working album, per-field action hits, errors
def check_idempotence(script, first: ScriptResult, ev) -> list[IdempotenceIssue]: ...
```
- Preview request: `{script_text | script, query, scope?, limit?}`; response:
  `{targets: [{target, label, fields: [{field, before, after, actions}], errors}], idempotence: [{target, field, action, first, second}], validation_errors, truncated}`.
- Profile folder: `<profile>/scripts/` (created on first save; `profile create` adds it).

As implemented (2026-11-12; `rifferr.tagscript`, `rifferr.steps.tagscript`, entry point
`tags.script`):
- `album_item_fields()` returns `SharedFields` (`include`/`exclude`, supports `in`), not a
  frozenset. beets' `Album.store` carries every flexible attribute as well as
  `Album.item_keys`, which a fixed set can't express. beets: everything but `id` and `artpath`.
- BACKEND-10: a snapshot field given as `None` counts as absent, with no fallback. The
  evaluator passes deleted fields that way, so a `delete` holds for later actions. One edge
  remains in beets: an item still sees its stored album's value for an album field deleted
  earlier in the script (B-035).
- BACKEND-11: `''` becomes the type's empty value. Lists pass through unchanged (`check`
  rejects them for single-value fields). Numbers become text in string fields, and
  booleans go only into boolean fields. For beets integer fields, `'abc'` is an error
  (beets' own `parse` would give 0).
- Item changes that only follow an album change of a shared field aren't proposed, because
  storing the album carries them and the journal still records them as effects. Preview and
  the idempotence check leave them out the same way.
- Rationale: the action's `name`, or `action N` (1-based). Several actions are joined with `, `.
- `case`: a word is a run of non-whitespace. `keep` matches the word without surrounding
  punctuation. `append_unique` skips empty values. `from_filename` takes `$field` or
  `${field}`, matches the whole text (non-greedy), counts as item-scoped in album scripts,
  and makes `scope: album` a validation error.
- A script file's `{{ }}` expressions are interpolated in its string values after YAML
  parsing, as inline actions are, so substituted text can't change the YAML structure.
- The step requires query, templates and apply. Outputs: `changesets`, `field_changes`,
  `not_idempotent`, `script_text` (inline: the actions as JSON), `script_path`,
  `script_sha256`. The summary is an `info` line per album
  (`album-7: 2 field changes proposed; idempotent`, `…: no changes; idempotence off`).
  Idempotence findings are `warning` lines (`error` with `fail`).
- New plugin API: `StepContext.profile_dir` and `ctx.interpolate(text)`.
- The safety report finding is kind `auto_apply`.
- Preview request: `{script_text | script, query, scope?, limit?, client?}`. `client` names
  the caller, and a newer request with the same `client` supersedes the older one. The
  response also has `album` (per target), `notes`, `deleted`, `idempotence_checked` and
  `albums` (evaluated). Script errors come back as 200 with `validation_errors`; a missing
  file is `404 script_not_found`. Preview interpolation sees the profile's vars and name;
  `run.id` is `preview` and `steps.*` aren't available.
- Read-only worker: `request` / `cancel_request` commands and `response` messages on the
  WORKER-9 channel. It answers one request at a time and checks for cancellation between
  albums. A worker that fails to start answers `503 preview_unavailable`.
- `rifferr profile create` adds `scripts/` next to `workflows/` (the PROFILE-8 test now
  expects both).
- Editor: a fixed 20-row monospace textarea. A new script starts from a short template, and
  `/scripts` has a "New script" field.
- The backend query returns every matching album before `limit` applies (B-034).
- The Playwright spec `frontend/e2e/tagscript.spec.ts` was written but couldn't run on the
  dev machine, which has no browser installed (B-032). Vitest covers the page. The beets
  path was smoke-tested by hand against a temp library.

## Examples
Illustrative (architecture §4), not the reference checklist's logic:
```yaml
# <profile>/scripts/ep-suffix.yaml
scope: album
actions:
  - name: ep-suffix
    when: 'albumtype:ep ^album::\(EP\)$'
    set: { album: '$album (EP)' }
  - replace: { fields: [title], find: '/', with: '∕' }
    scope: item
```
```yaml
- id: release-types
  uses: tags.script
  apply: review
  with: { script: ep-suffix.yaml }
```
Album `album-7` (`albumtype: ep`, `album: Live Sessions`, a track titled `A/B`)
→ one ChangeSet: album `album → "Live Sessions (EP)"` (rationale `ep-suffix`),
`item-12 title "A/B" → "A∕B"` (rationale `action 2`). Idempotence: second pass
changes nothing. On a re-run after apply, nothing is proposed.

Without the `^album::\(EP\)$` guard, preview shows the second pass producing
`Live Sessions (EP) (EP)` and highlights action `ep-suffix`; a run with
`idempotence: warn` logs it, with `fail` it proposes nothing for that album.

## Extensibility
- New action types are not pluggable in M1; the `python:` escape hatch (architecture §4) is a later spec.
- Template functions come from beets and from Rifferr's template-function plugins (entry points), so scripts can use them in `set` and `when`.
- Case `keep` lists and other conventions live in scripts or rules files, never in code.

## Security & safety
- Scripts only propose; the engine applies (non-negotiable 3).
- Script paths stay inside the profile's `scripts/` folder (TAGSCRIPT-1, API-18).
- The read-only worker can't write the library (BACKEND-13); preview never proposes.
- Regexes come from the user's own scripts. A catastrophic regex is bounded by the preview timeout and the run's cancellation (RUN-8); a run's `tags.script` step checks cancellation between albums.

## Non-goals
- Visual action-list builder and condition builder (architecture §4 UI requirements): a later spec (B-028).
- Syntax highlighting or autocomplete in the editor (B-028).
- `python:` actions, custom action plugins.
- File moves or renames from scripts (`beets.command move`, S-BEETS-COMMAND).
- Per-album lanes (M2).

## Open questions
- ~~Reference "release-type suffix" logic~~ Resolved 2026-11-13 (author): `albumtype` (MusicBrainz) decides EP / Single; an album name ending in the word EP / Single (any case, optionally in parentheses or brackets) sets `albumtype` to match; EPs and Singles whose name doesn't end that way get " (EP)" / " (Single)" (for players that don't read `albumtype`). Only the end of the name counts. Albums whose `albumtypes` include compilation get " (Compilation)" through review. Later, not M1: a track-count and running-time guess sent to review (B-044), Vinyl from rip logs (B-045), 24-bit in folder names only (B-046). Implemented as `examples/scripts/release-types.yaml` and `compilation.yaml`, used by `examples/workflows/foobar-style.yaml`.
- ~~Visual builder in M1?~~ Resolved 2026-11-11 (author left it to the agent): no; M1 ships the YAML editor with live preview (TAGSCRIPT-22). The visual action-list and condition builders get their own later spec (B-028).
- ~~Editor component?~~ Resolved 2026-11-11 (author left it to the agent): a plain monospace Mantine `Textarea` in M1, so no new dependency and no bundle growth (B-027). A code editor (e.g. CodeMirror 6, MIT) is considered with the visual builder (B-028).

## Tasks
- [x] action models + published `tagscript.schema.json`
- [x] pure evaluator: ordered actions, working values, album-to-item sharing
- [x] each action + Hypothesis idempotence properties
- [x] idempotence check with responsible action
- [x] `tags.script` step: script files, net diff, `ctx.propose`, summary log, step record
- [x] C-BACKEND amendments (BACKEND-10 to -13) for Fake and beets + contract tests
- [x] read-only worker (WORKER-11)
- [x] preview API + scripts API (API-18)
- [x] script editor page with live preview grid + Playwright test (spec written; not run here, see As implemented)
- [x] safety report finding for `apply: auto`
