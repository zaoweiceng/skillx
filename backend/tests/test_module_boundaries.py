"""Guard the dependency direction established by the runtime refactor."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_domain_does_not_depend_on_application_or_infrastructure():
    for path in (APP / "domain").glob("*.py"):
        assert not [
            name
            for name in imports(path)
            if name.startswith(("app.application", "app.infrastructure", "app.api", "app.runtime"))
        ], path


def test_scheduler_and_host_do_not_import_concrete_agent_transports():
    for name in ("orchestrator", "agent_host", "agent_run"):
        path = APP / "application" / f"{name}.py"
        assert not set(imports(path)) & {"app.runtime.traex_builtin", "app.runtime.adapters"}, path


def test_execution_sources_do_not_depend_on_worker():
    assert "app.application.orchestrator" not in set(
        imports(APP / "application/execution_sources.py")
    )
