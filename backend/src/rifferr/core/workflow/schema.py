"""JSON Schema for workflow files (WORKFLOW-12).

Step entries become a ``oneOf`` keyed on ``uses``, each with that step type's
``with:`` schema, so editors can autocomplete parameters.
"""

from __future__ import annotations

import copy
from typing import Any, cast

from rifferr.core.workflow.models import Workflow
from rifferr.core.workflow.registry import StepRegistry

JsonSchema = dict[str, Any]


def workflow_json_schema(registry: StepRegistry) -> JsonSchema:
    schema = Workflow.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Rifferr workflow"
    defs = cast(dict[str, JsonSchema], schema.setdefault("$defs", {}))
    # Name the step definition "Step" (what users see), not the class name.
    defs["Step"] = step = defs.pop("StepSpec")
    steps_prop = cast(dict[str, JsonSchema], schema["properties"])["steps"]
    steps_prop["items"] = {"$ref": "#/$defs/Step"}
    variants: list[JsonSchema] = []
    for name in registry.names():
        info = registry.get(name)
        if info is None:
            continue
        params_def = "With_" + name.replace(".", "_").replace("-", "_")
        params = info.params_model.model_json_schema(
            by_alias=True, ref_template=f"#/$defs/{params_def}__{{model}}"
        )
        for sub_name, sub in cast(dict[str, JsonSchema], params.pop("$defs", {})).items():
            defs[f"{params_def}__{sub_name}"] = sub
        defs[params_def] = params
        variant = copy.deepcopy(step)
        props = cast(dict[str, JsonSchema], variant["properties"])
        props["uses"] = {"const": name, "title": "Uses"}
        props["with"] = {"$ref": f"#/$defs/{params_def}"}
        variant["title"] = f"Step: {name}"
        variants.append(variant)
    if variants:
        defs["Step"] = {"title": "Step", "oneOf": variants}
    return schema
