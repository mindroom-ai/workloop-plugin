from __future__ import annotations

import sys
from importlib import util
from pathlib import Path
from uuid import uuid4

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"


def _load_tools_module():
    tools_path = Path(__file__).resolve().parents[1] / "tools.py"
    module_name = f"{PACKAGE_NAME}.tools_test_{uuid4().hex}"
    sys.modules.pop(module_name, None)
    spec = util.spec_from_file_location(module_name, tools_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
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
