# C-TEMPLATE-FUNCTIONS: Rifferr template functions in beets templates
Status: Implemented
Milestone: M5

## Context
Architecture §1 and §4: "Rifferr template functions are registered as beets template functions,
so they also work in beets path formats." The entry-point group `rifferr.template_functions`
exists (`rifferr/plugins.py`) but nothing loads it into beets yet.

The reference naming pattern needs one function beets doesn't have: foobar2000's
`$ifgreater(%totaldiscs%,1,Disc %discnumber%/,)` adds a disc folder only for multi-disc albums.
beets' `%if{$disctotal,…}` can't do it because `1` is true. The beets `inline` plugin could, but
it runs Python from config, which needs config trust (SEC-62). A small built-in function is the
general fix (non-negotiable 7: built-ins use the public plugin API).

Used by S-LIBRARY-MOVE (path formats) and S-TAGSCRIPT (`set` templates, `when` is unaffected).

## Behavior
- TEMPLATE-FUNCTIONS-1: The beets worker MUST load every `rifferr.template_functions` entry point
  at startup and register each as a beets template function under its entry-point name, so it
  works in tag-script templates, `shell` args, path formats and `beets.command` subprocesses
  alike.
- TEMPLATE-FUNCTIONS-2: Registration MUST go through beets' public plugin mechanism
  (`BeetsPlugin.template_funcs`), via one Rifferr beets plugin module inside
  `rifferr.backends.beets`, loaded by name (`rifferr`) as a beets plugin. The worker adds it to
  the plugin list when the profile's beets config doesn't list it. beets is not patched; beets
  imports plugins as `beetsplug.<name>`, so the package ships a `beetsplug/rifferr` shim.
- TEMPLATE-FUNCTIONS-3: For `beets.command` subprocesses, the same plugin MUST be importable by
  the `beet` CLI, which gets the worker's plugin list with `-p`, so path formats evaluate
  identically in-process and in the CLI.
- TEMPLATE-FUNCTIONS-4: A function name that collides with a beets built-in or another plugin's
  function MUST fail worker startup with a message naming both providers.
- TEMPLATE-FUNCTIONS-5: A template function MUST be a pure function of its string arguments:
  no filesystem, network or library access. An exception inside it MUST surface as a template
  error naming the function (beets itself inlines `<message>` into the output; the backend
  raises instead) (TAGSCRIPT-15 for scripts; LIBRARY-MOVE-5 for path planning).
- TEMPLATE-FUNCTIONS-6: Built-in `%ifgreater{a,b,then,else}` MUST return `then` when `a` and `b`
  both parse as numbers and `a > b`, else `else` (empty when omitted). Non-numeric `a` or `b`
  returns `else`.
- TEMPLATE-FUNCTIONS-7: Built-in `%unpad{n}` MUST strip leading zeros from a whole number
  (`01` → `1`, `00` → `0`), so a path matches foobar2000's unpadded `%discnumber%`. Text that
  isn't a whole number MUST come back unchanged (added 2026-11-18, author: S-LIBRARY-MOVE
  question 2).

## Interfaces
```toml
[project.entry-points."rifferr.template_functions"]
ifgreater = "rifferr.tagscript.functions:ifgreater"
unpad = "rifferr.tagscript.functions:unpad"
```
```python
def ifgreater(a: str, b: str, then: str = "", else_: str = "") -> str: ...
def unpad(n: str) -> str: ...
```

## Examples
- `%ifgreater{$disctotal,1,Disc %unpad{$disc}/}` with `disctotal=2, disc=1` → `Disc 1/` (beets
  pads `$disc` to `01`); with `disctotal=1` → empty.
- `%ifgreater{x,1,yes,no}` → `no`.

## Extensibility
Third-party packages add functions through the same entry point. The registration path, the
purity rule and the collision rule stay fixed.

## Security & safety
Functions are installed Python packages, the same trust level as step plugins. No function can
be defined from workflow or config text (that remains the `inline` plugin, behind config trust).

## Non-goals
- Porting foobar2000's title-formatting language.
- Template functions with library or filesystem access.

## Open questions
Resolved: built-ins live in `rifferr.tagscript.functions` (pure layer); the beets plugin in
`rifferr.backends.beets.templates`.

## Tasks
- [x] Rifferr beets plugin registering `rifferr.template_functions` (TEMPLATE-FUNCTIONS-1..4).
- [x] `ifgreater` built-in with Hypothesis tests.
- [x] `unpad` built-in with Hypothesis tests (TEMPLATE-FUNCTIONS-7).
- [x] Contract test: same function result in-process and through the `beet` CLI.
