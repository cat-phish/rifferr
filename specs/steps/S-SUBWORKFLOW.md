# S-SUBWORKFLOW: `workflow` step (call another workflow)
Status: Implemented
Milestone: M2

## Context
Architecture §1 says a workflow "can call other workflows (`uses: workflow`)", and §3 lists
the `workflow` step as "Call another workflow. Variables passed explicitly." Users who run
several import workflows (an inbox watch, a webhook for one downloader, a manual re-tag) repeat
the same tagging, cover and cleanup steps in each. A call lets them keep those steps in one file
and change them once. C-WORKFLOW lists `uses: workflow` as deferred to this spec.

A called workflow runs inside the caller's run, not as a separate run. Its steps act on the
caller's albums, take part in lanes (C-LANES) and holds like the caller's own steps, and show
in the same run log, run page and review queue. That keeps "one album, one lane" true across a
call, and needs no second worker or run-to-run waiting.

Links: architecture §1, §3; C-WORKFLOW; C-RUN; C-LANES; F-SECURITY (SECURITY-10 to -12);
S-TAGSCRIPT (script paths, the model for workflow paths).

## Behavior

### Calling
- SUBWORKFLOW-1: A step `uses: workflow` MUST name the called workflow with `with.workflow`, a
  path relative to the profile's `workflows/` folder. A path that is absolute, contains `..`
  segments that leave the folder, or contains `{{ }}` MUST be a validation error. The file MUST
  exist and validate (C-WORKFLOW) when the caller is validated.
- SUBWORKFLOW-2: `with.vars` MUST be a map from the called workflow's declared var names to
  values. A name the called workflow doesn't declare MUST be a validation error. A declared var
  with no default that `with.vars` doesn't supply MUST be a validation error. Profile vars are
  not passed implicitly: inside the called workflow, `{{ vars.X }}` is only what the call
  passes, else the var's default (architecture §3: "variables passed explicitly").
- SUBWORKFLOW-3: Values in `with.vars` MUST be interpolated in the caller's context (its
  `vars`, `steps`, `run`, `profile`) each time a called step starts, like any `with:` value
  (WORKFLOW-8). So a value may use the outputs of the caller's earlier steps.
- SUBWORKFLOW-4: Inside a called workflow, `{{ steps.<id>.<key> }}` MUST resolve against the
  called workflow's own steps only; `run` and `profile` are the caller's run and profile.
- SUBWORKFLOW-5: A workflow that calls itself directly or through other calls MUST be a
  validation error naming the cycle. Calls MUST nest at most 8 deep.

### Running
- SUBWORKFLOW-6: At submit, the engine MUST expand each call into the called workflow's steps,
  in place and in file order, recursively. Expanded steps keep their own `uses`, `with`, `when`,
  `apply`, `on_error` and `run_level`. Their step id in events, step records, lanes and the API
  MUST be the call path joined with `/` (`tagging/fix-case`, `post/tagging/fix-case`); `/` is
  not allowed in a written step id, so these never clash with the caller's ids.
- SUBWORKFLOW-7: A call's `when:` MUST narrow every called step: a called step acts on targets
  that match both the call's `when` and its own (and the call's ancestors' `when`s).
- SUBWORKFLOW-8: A call step MUST NOT take `apply` or `on_error`: each called step's own values
  apply. Setting either on a `uses: workflow` step MUST be a validation error.
- SUBWORKFLOW-9: Lanes, holds, review gates, `apply: review` pauses, cleanup decisions and
  failures inside a called workflow MUST behave exactly as if the steps were written in the
  caller (C-LANES). A resume after a hold inside a call continues from that called step.
- SUBWORKFLOW-10: The called workflow's `on:` triggers and `input` MUST be ignored when it is
  called; they apply only when it runs on its own. Its `name` and `description` are used only
  for labels (the run page groups called steps under the call, with the called workflow's name).
- SUBWORKFLOW-11: A called workflow MAY declare `outputs:`, a map from output name to a
  `{{ steps.<id>.<key> }}` expression over its own steps. The call step's outputs MUST be those
  values, resolved once its last called step has finished for the run, and the caller reads
  them as `{{ steps.<call-id>.<name> }}` (RUN-12). Referencing an output the called workflow
  doesn't declare MUST be a validation error. A workflow run on its own ignores `outputs:`.

### Trust and safety
- SUBWORKFLOW-12: When a run is submitted, the job MUST store the text of the caller and of
  every workflow file it calls, with each file's hash, and the worker MUST run those stored
  texts. Editing a called file after submit changes neither a queued run nor a resumed one.
- SUBWORKFLOW-13: A run MUST be trusted (SECURITY-10) only when the caller and every workflow it
  calls are trusted by content hash. Trusting a caller MUST NOT trust the files it calls. The
  trust prompt in the UI and `rifferr trust` MUST list untrusted called files and let the user
  trust them together, each by its own hash.
- SUBWORKFLOW-14: The static safety report (SECURITY-11) for a caller MUST include the findings
  of every workflow it calls, each labelled with its expanded step id and the called file.
- SUBWORKFLOW-15: `RunSummary` / `GET /api/runs/{id}` MUST record the hash of each called file
  next to the workflow hash (RUN-11).

## Interfaces

```python
class WorkflowCallParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: str = Field(description="Workflow file, relative to the profile's workflows/ folder")
    vars: dict[str, str | int | float | bool] = Field(default_factory=dict)

class Workflow(...):              # C-WORKFLOW, additive
    outputs: dict[str, str] = {}  # name -> "{{ steps.<id>.<key> }}"
```

- Step type `workflow`, registered through the `rifferr.steps` entry point like every built-in.
  A written call never runs: the engine expands it (SUBWORKFLOW-6), and its `run` fails if
  reached unexpanded. When the called workflow declares `outputs:`, expansion adds the call
  step itself after the called steps, with the call's id; it resolves the outputs for its
  lanes (SUBWORKFLOW-11). Without `outputs:` the call leaves no step of its own.
- A call's `when` narrows by album: the engine queries the run's albums with it at album
  scope and passes only the matching ones to the called step (SUBWORKFLOW-7).
- Expansion lives in `core/workflow` (pure: it takes a loader callable for called files), so
  `rifferr validate`, the API's validate route and the worker share it.
- Job payload: `source`, `source_name` as now, plus `called: [{path, source, sha256}]`.
- Events and step records use the expanded step id; `run.step.started` also carries
  `call_path` (list of call step ids) and `workflow` (the called file's path) for called steps.
- Schema: `schemas/workflow.schema.json` gains `outputs` and the `workflow` step's `with:`
  schema (additive, no break).

## Examples

`workflows/tagging.yaml`:
```yaml
name: Tagging
vars:
  genre_rules: {default: rules/genres.yaml}
  case_style: {description: "title or sentence"}
outputs:
  changed: "{{ steps.fix-case.changed }}"
steps:
  - id: fix-case
    uses: tags.script
    with: {script: case.yaml, vars: {style: "{{ vars.case_style }}"}}
    apply: review
  - id: genres
    uses: tags.script
    with: {script: genres.yaml}
```

`workflows/inbox.yaml`:
```yaml
name: Inbox import
on: {watch: {path: /srv/inbox}}
steps:
  - id: import
    uses: beets.import
    with: {path: "{{ run.input }}"}
  - id: tagging
    uses: workflow
    with:
      workflow: tagging.yaml
      vars: {case_style: title}
  - id: tell
    uses: notify
    with: {message: "{{ steps.tagging.changed }} albums retagged"}
```

Run step ids: `import`, `tagging/fix-case`, `tagging/genres`, `tagging` (the call's outputs),
`tell`. An album whose
`tagging/fix-case` ChangeSet waits for review holds only its own lane; other albums go on to
`tagging/genres` and `tell`.

Errors:
- `with: {workflow: ../other/x.yaml}` → "workflow '../other/x.yaml' leaves the profile's
  workflows/ folder".
- `tagging.yaml` calls `inbox.yaml` → "workflow call cycle: inbox.yaml → tagging.yaml →
  inbox.yaml".
- `vars: {case: title}` → "tagging.yaml declares no var 'case'"; missing `case_style` →
  "tagging.yaml needs var 'case_style'".

## Extensibility
- Called workflows can use any registered step type, plugins included.
- What stays fixed: expansion is done by the engine, not by a plugin; `workflow` can't be
  overridden by a plugin step of the same name (registry names are unique).

## Security & safety
- Trust is per file and never inherited (SUBWORKFLOW-13): an untrusted called file keeps its
  shell and python steps from running even if the caller is trusted.
- Paths stay inside `workflows/` (SUBWORKFLOW-1); a called file can't be outside the profile.
- The run executes the texts stored at submit (SUBWORKFLOW-12), so an edit between the trust
  check and the run can't slip in untrusted steps.
- Cycles and depth are checked before any run (SUBWORKFLOW-5), so a call can't recurse forever.

## Non-goals
- Calling a workflow of another profile, or by URL.
- Running a called workflow as its own run (parallel, or in another worker).
- Dynamic calls (the file chosen by `{{ }}`), loops, or calling once per album with different
  vars.
- Passing albums explicitly: called steps act on the run's selection, as the caller's do.

## Open questions
1. ~~Expansion versus a nested run?~~ Resolved 2026-11-14 (author): expand in place
   (SUBWORKFLOW-6).
2. ~~Maximum depth?~~ Resolved 2026-11-14 (author): keep a limit, 8 (SUBWORKFLOW-5).
3. ~~Trust per file, or the caller trusts the files it calls?~~ Resolved 2026-11-14 (author):
   per file (SUBWORKFLOW-13).
4. ~~Profile vars implicit in called workflows?~~ Resolved 2026-11-14 (author): no, vars are
   passed explicitly (SUBWORKFLOW-2).
5. ~~`outputs:` now or later?~~ Resolved 2026-11-14 (author): now (SUBWORKFLOW-11).

## Tasks
- [x] C-WORKFLOW: `outputs` field; `/` reserved in step ids; schema regenerated
- [x] `core/workflow` expansion: path rules, var binding, cycle and depth checks, `when` merge
- [x] Scoped interpolation contexts for called steps (vars, steps)
- [x] `workflow` step type and entry point; validation of `apply`/`on_error` on calls
- [x] Job payload carries called files; worker runs stored texts; resume uses them
- [x] Trust over every file; trust prompt and `rifferr trust` list called files
- [x] Safety report over called files
- [x] Events and API: expanded step ids, `call_path`, called file hashes
- [x] Run page groups called steps under their call
- [x] Example workflow: move a reusable part of `foobar-style.yaml` into a called file
- [x] Tests per behavior ID; Playwright for the trust prompt with a called file
