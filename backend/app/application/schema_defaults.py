"""Apply explicit object/array defaults consistently before executing inputs."""
from copy import deepcopy


def apply_input_defaults(value, schema):
    value = deepcopy(value)
    if not isinstance(schema, dict):
        return value
    if isinstance(value, dict):
        for key, child in schema.get("properties", {}).items():
            if not isinstance(child, dict):
                continue
            if key not in value and "default" in child:
                value[key] = deepcopy(child["default"])
            if key in value:
                value[key] = apply_input_defaults(value[key], child)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        value = [apply_input_defaults(item, schema["items"]) for item in value]
    return value
