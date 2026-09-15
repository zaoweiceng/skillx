"""Project a durable checkpoint into only the data needed by its remaining graph."""

import json
import re
from typing import Any

_REFERENCE = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_-]*)\.([^}]+)\}")


def relevant_checkpoint_values(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    nodes = snapshot.get("remainingNodes", [])
    references = _REFERENCE.findall(json.dumps(nodes, ensure_ascii=False))
    sources = {source for source, _ in references if source != "input"}
    sources.update(edge.get("source") for edge in snapshot.get("remainingEdges", []))
    outputs = {
        key: value for key, value in snapshot.get("completedOutputs", {}).items() if key in sources
    }
    input_paths = {
        re.split(r"[.\[]", path, maxsplit=1)[0] for source, path in references if source == "input"
    }
    inputs = {
        key: value for key, value in snapshot.get("runInput", {}).items() if key in input_paths
    }
    return outputs, inputs
