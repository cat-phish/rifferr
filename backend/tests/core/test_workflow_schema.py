from __future__ import annotations

import json
from typing import Any

from rifferr.core.workflow import StaticStepRegistry, workflow_json_schema
from rifferr.schemas import export


def test_WORKFLOW_12_schema_has_uses_discriminated_step_variants(
    registry: StaticStepRegistry,
) -> None:
    schema = workflow_json_schema(registry)
    step = schema["$defs"]["Step"]
    variants: list[dict[str, Any]] = step["oneOf"]
    uses = sorted(v["properties"]["uses"]["const"] for v in variants)
    assert uses == ["beets.import", "shell"]
    shell = next(v for v in variants if v["properties"]["uses"]["const"] == "shell")
    with_ref = shell["properties"]["with"]["$ref"]
    with_schema = schema["$defs"][with_ref.removeprefix("#/$defs/")]
    assert set(with_schema["properties"]) == {"run", "per", "timeout"}
    assert with_schema["required"] == ["run"]
    json.dumps(schema)  # serializable


def test_WORKFLOW_12_schema_without_registered_steps_accepts_any_with() -> None:
    schema = workflow_json_schema(StaticStepRegistry([]))
    assert "oneOf" not in schema["$defs"]["Step"]
    assert schema["$defs"]["Step"]["properties"]["uses"]["type"] == "string"


def test_WORKFLOW_12_schema_core_shape() -> None:
    schema = workflow_json_schema(StaticStepRegistry([]))
    assert schema["required"] == ["name", "steps"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"name", "description", "vars", "on", "steps"}
    step = schema["$defs"]["Step"]
    assert step["additionalProperties"] is False
    assert step["properties"]["apply"]["default"] == "review"


def test_WORKFLOW_12_exported_with_other_schemas() -> None:
    assert "workflow.schema.json" in export.render()
