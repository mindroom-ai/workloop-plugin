from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_tools_module():
    tools_path = Path(__file__).resolve().parents[1] / "tools.py"
    spec = importlib.util.spec_from_file_location("workloop_tools_test", tools_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_add_todo_schema_includes_title_parameter() -> None:
    module = _load_tools_module()

    manager = module.WorkloopTodoManager()
    function = manager.functions["add_todo"]
    function.process_entrypoint()
    schema = function.parameters

    assert "title" in schema["properties"]
    assert schema["properties"]["title"]["type"] == "string"
    assert "title" in schema["required"]
    assert "agent" not in schema["properties"]
