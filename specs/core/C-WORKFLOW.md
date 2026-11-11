# C-WORKFLOW: Workflow file schema, validation and interpolation
Status: Implemented
Milestone: M0

## Context
Workflows are plain YAML files users write by hand, share and keep in git
(architecture §1, §2). This spec defines their shape, how they are validated,
and how `{{ ... }}` workflow-context interpolation works. It lives in
`rifferr.core` (pure). Step-specific `with:` parameters are defined by each step
spec; this spec only defines the envelope.

## Behavior
- WORKFLOW-1: A workflow file MUST parse into a `Workflow` model with `name`, optional `description`, optional `vars`, optional `on`, and a non-empty ordered `steps` list.
- WORKFLOW-2: Each step MUST have `id` (unique within the workflow, matching `^[a-z][a-z0-9_-]*$`) and `uses` (a registered step type name); `with`, `when`, `apply`, `on_error` and `name` are optional.
- WORKFLOW-3: `apply` MUST default to `review` and accept only `auto` or `review`; `on_error` MUST default to `stop` and accept only `stop`, `skip` or `continue`.
- WORKFLOW-4: Unknown top-level keys and unknown step keys MUST be validation errors that name the key and its line and column.
- WORKFLOW-5: Validation MUST check each step's `with:` against the parameter model the step type publishes, and report errors with the step `id` and the YAML location.
- WORKFLOW-6: Validation MUST check that every step type's required backend capabilities are offered by the target profile's backend, when a backend is given.
- WORKFLOW-7: Declared `vars` MAY have a default and a description; a workflow that references `{{ vars.X }}` where `X` is neither declared nor supplied by the profile MUST fail validation.
- WORKFLOW-8: `{{ ... }}` expressions MUST be resolved when a step starts, from a context with exactly the roots `vars`, `run`, `profile` and `steps` (outputs of earlier steps). Expressions are dotted paths only, no code, filters or function calls.
- WORKFLOW-9: Interpolation MUST NOT touch `$field` or `%func{}` beets template syntax; a string containing both is passed on with only the `{{ }}` parts replaced.
- WORKFLOW-10: A literal `{{` MUST be writable as `{{ '{{' }}`. Any other malformed `{{` expression MUST be a validation error.
- WORKFLOW-11: Loading and re-saving a workflow through the API MUST preserve comments, key order and quoting (ruamel.yaml round-trip).
- WORKFLOW-12: The JSON Schema for workflow files MUST be generated from the Pydantic models into `schemas/workflow.schema.json`, including each registered step type's `with:` schema under a `uses` discriminator.
- WORKFLOW-13: The `on:` block MUST be parsed and validated (trigger names `watch`, `schedule`, `webhook`) but not acted on in M0.

## Interfaces
```python
class StepSpec(BaseModel):
    id: str
    uses: str
    name: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    when: str | None = None              # backend condition syntax (beets query)
    apply: Literal["auto", "review"] = "review"
    on_error: Literal["stop", "skip", "continue"] = "stop"

class VarDecl(BaseModel):
    default: str | None = None
    description: str | None = None

class Workflow(BaseModel):
    name: str
    description: str | None = None
    vars: dict[str, VarDecl] = {}
    on: Triggers | None = None
    steps: list[StepSpec]

def load_workflow(text: str, source: str) -> tuple[Workflow, RoundTripDoc]: ...
def validate(wf: Workflow, registry: StepRegistry,
             backend_caps: BackendCapabilities | None,
             profile_vars: Mapping[str, str]) -> list[ValidationError]: ...
def interpolate(value: str, ctx: InterpolationContext) -> str: ...
```
`ValidationError` carries `path` (e.g. `steps[1].with.run`), `line`, `column`, `message`.

As implemented (`rifferr.core.workflow`):
- `validate(wf, registry, backend_caps=None, profile_vars=None, *, doc=None)`. Pass the
  `RoundTripDoc` from `load_workflow` to get line/column. `profile_vars=None` skips the
  profile-dependent var checks; `backend_caps=None` skips capability checks.
- Fields allowed under the fixed roots: `run.id`, `run.workflow`, `profile.name`.
  `vars.<name>` and `steps.<id>.<key>` as described above.
- `RoundTripDoc.dump()` returns the original text until `set()` edits it. After an edit,
  ruamel.yaml keeps comments, key order, quoting and block layout; the one normalization is
  whitespace inside flow mappings (`{ a: b }` becomes `{a: b}`).
- `BackendCapabilities` (BACKEND-2) landed in `rifferr.backends.base` with this spec, because
  WORKFLOW-6 needs it.

## Examples
```yaml
name: two-step
vars:
  inbox: { description: "Folder new downloads land in" }
steps:
  - id: import
    uses: beets.import
    with: { path: "{{ vars.inbox }}", mode: quiet }
  - id: notify-done
    uses: shell
    apply: auto
    with:
      run: ["echo", "imported into run {{ run.id }}: $albumartist - $album"]
      per: album
```
`{{ vars.inbox }}` → profile value; `$albumartist - $album` is left for the backend.

## Extensibility
Step types supply their own `with:` model. Interpolation roots and syntax are fixed.

## Security & safety
- Interpolation is path lookup only: no evaluation, so a workflow file cannot run code through `{{ }}`.
- Secrets are not reachable from `{{ }}` (the `profile` root exposes non-secret fields only).

## Non-goals
- `uses: workflow` calls (S-SUBWORKFLOW, M2). Triggers acting on `on:` (F-TRIGGERS, M2).
- Workflow trust hashing (F-SECURITY).

## Open questions
- ~~Does `steps.<id>.<output>` need to exist in M0?~~ Resolved 2026-11-11 (author): yes as syntax; validation checks `<id>` is an earlier step. Values are filled once C-RUN step results exist.
- ~~File extension and discovery?~~ Resolved 2026-11-11 (author): `*.yaml` and `*.yml`; default location `<profile>/workflows/`. Discovery itself belongs to F-API / F-CLI.
- ~~`apply` on steps that never produce ChangeSets?~~ Resolved 2026-11-11 (author): allowed and ignored; no error, no warning.
- Dependency ruamel.yaml (MIT) approved 2026-11-11 (author).

## Tasks
- [x] models + loader with line/column errors
- [x] validation (params, capabilities, vars)
- [x] interpolation + tests for mixed `{{ }}` / `$field` strings
- [x] round-trip save
- [x] schema export
