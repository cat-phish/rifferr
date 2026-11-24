# C-TEMPLATE-CONDITIONS: Template conditions on tag-script actions
Status: Implemented
Milestone: M5

## Context
Architecture §4 (tag scripts) and C-TEMPLATE-FUNCTIONS. An action's `when` is a beets query:
it compares a field with fixed text, so it can't compare two fields. Checklist line 10
(`docs/coverage.md`, B-084) needs exactly that: splits and collaborations carry their artists
in the album name, "A / B - Split EP" (author, 2026-11-23), and the script that adds them must
not add them again on a rerun (TAGSCRIPT-19 idempotence). The author chose a template
condition plus a string template function over a hidden once-per-album marker (2026-11-23).

A beets template can already see every field of the target, so a template condition covers
field-to-field checks in general. C-ALBUM-FACTS (ALBUM-FACTS-4) dropped an `if:` key for
facts because typed facts fit `when`; this spec adds `if:` / `unless:` for what queries can't
express.

## Behavior
- TEMPLATE-CONDITIONS-1: A tag-script action MAY have `if:` and/or `unless:`, each a beets
  template evaluated on the target's working values (TAGSCRIPT-3), album facts included when
  the template names one (ALBUM-FACTS-3).
- TEMPLATE-CONDITIONS-2: A template result is *true* unless, with surrounding whitespace
  removed, it is empty, `0` or `false` (case-insensitive). The action runs only when `when`
  matches (if given), `if` is true (if given) and `unless` is false (if given).
- TEMPLATE-CONDITIONS-3: A template error in `if` or `unless` MUST fail the album like any
  action error (TAGSCRIPT-15), naming the action and the key.
- TEMPLATE-CONDITIONS-4: Built-in template function `%startswith{text,prefix}` MUST return
  `1` when `text` starts with `prefix` (exact, case-sensitive) and an empty string otherwise.
  An empty `prefix` returns `1`.
- TEMPLATE-CONDITIONS-5: Rifferr's built-in template functions (`%ifgreater`, `%unpad`,
  `%startswith`) MUST work on FakeBackend too, so engine and step tests can use them; they
  live in `rifferr.tagscript.functions` (pure).
- TEMPLATE-CONDITIONS-6: `if` / `unless` MUST be accepted wherever tag-script actions are:
  script files, inline `tags.script` actions, `tags.choose` options and rules files
  (S-TAGS-CHOOSE), and the tag-script preview shows a skipped action as not matching.
- TEMPLATE-CONDITIONS-7: The published tag-script schema MUST mark `if` / `unless` as beets
  templates (the editor's template hint, as for `set` values).
- TEMPLATE-CONDITIONS-8: `examples/scripts/kind-album-name.yaml` MUST turn a split or
  collaboration's album name into `<albumartists joined by " / "> - <album>` once, with a
  table test and a Hypothesis idempotence test, and run in foobar-style after `album-kind`
  and before `path-characters`.

## Interfaces
```python
class ActionBase(BaseModel):
    when: str | None = None      # beets query
    if_: str | None = Field(default=None, alias="if")     # beets template
    unless: str | None = None                             # beets template
    scope: ActionScope | None = None
    name: str | None = None
```
```python
def startswith(text: str, prefix: str) -> str: ...   # "1" or ""
```

## Examples
`examples/scripts/kind-album-name.yaml`:
```yaml
scope: album
idempotence: fail
actions:
  # A helper field with the artists, removed again at the end: the net change is the album.
  - name: kind-artists
    when: "albumkind::^(split|collab)$"
    join: { field: albumartists, with: " / ", into: kind_artists }
  - name: artists-in-album-name
    when: "albumkind::^(split|collab)$ kind_artists::."
    unless: "%startswith{$album,$kind_artists - }"
    set: { album: "$kind_artists - $album" }
  - name: drop-helper
    delete: [kind_artists]
```
| album | albumkind | albumartists | → album |
|---|---|---|---|
| Split EP | split | [A, B] | A / B - Split EP |
| A / B - Split EP | split | [A, B] | (unchanged) |
| Live | (none) | [A] | (unchanged) |

## Extensibility
Plugin template functions (C-TEMPLATE-FUNCTIONS) work in `if` / `unless` on the beets backend.
The truth rule (TEMPLATE-CONDITIONS-2) is fixed.

## Security & safety
Templates run no code beyond template functions, which are pure (TEMPLATE-FUNCTIONS-5).
Values render into the template result only; they are never parsed again as templates.

## Non-goals
- Boolean expressions beyond `when` + `if` + `unless`.
- More string functions (`%endswith`, `%equals`, `%contains`) until a script needs them.

## Open questions
1. ~~Delete of an absent field?~~ Resolved 2026-11-23 (author approved as drafted): it stays a
   silent no-op (TAGSCRIPT-4) and shows nothing in previews.
2. ~~Helper field name?~~ Resolved 2026-11-23 (author approved as drafted): `kind_artists`, no
   reserved prefix.

As implemented (2026-11-23):
- FakeBackend's template renderer is now a small parser: `$field`, `${field}`, the `$`
  escapes and the functions in `rifferr.tagscript.functions.BUILT_INS`; any other
  `%func{}` is still a template error there. beets' own `%if`, `%the` etc. stay beets-only.
- `kind-album-name.yaml` also requires `albumartists::.` before joining, so an album with
  a kind but no artist list is left alone.

## Tasks
- [x] `if` / `unless` in the action model, evaluator and preview; schema regenerated
- [x] `%startswith` built-in; built-ins on FakeBackend
- [x] Tests per behavior (FakeBackend and beets contract)
- [x] `kind-album-name.yaml`, idempotence test, foobar-style step, coverage row 10
