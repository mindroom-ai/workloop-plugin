from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from importlib import util
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest
from pydantic import ValidationError

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"


@dataclass(frozen=True, slots=True)
class _FakeRuntimePaths:
    storage_root: Path
    config_dir: Path


@dataclass(frozen=True, slots=True)
class _FakePrivateConfig:
    per: str


@dataclass(frozen=True, slots=True)
class _FakeAgentConfig:
    private: _FakePrivateConfig | None


@dataclass(frozen=True, slots=True)
class _FakePluginEntry:
    path: str
    enabled: bool = True
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _FakeConfig:
    agents: dict[str, _FakeAgentConfig]
    plugins: list[_FakePluginEntry]


@dataclass(frozen=True, slots=True)
class _FakeRuntimeContext:
    agent_name: str
    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    requester_id: str
    runtime_paths: _FakeRuntimePaths
    config: _FakeConfig


@dataclass(frozen=True, slots=True)
class _FakeExecutionIdentity:
    agent_name: str


@dataclass(frozen=True, slots=True)
class _FakeWorkspace:
    root: Path


@dataclass(frozen=True, slots=True)
class _FakeAgentRuntime:
    workspace: _FakeWorkspace | None


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


def _write_template(template_dir: Path, name: str, body: str) -> None:
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / f"{name}.yaml.j2").write_text(dedent(body).lstrip(), encoding="utf-8")


def _bind_scope(monkeypatch: pytest.MonkeyPatch, module, state_root: Path) -> None:
    monkeypatch.setattr(
        module,
        "_current_scope",
        lambda _runtime_paths: (state_root, "!room:test", "$thread:test", "codex"),
    )


def _bind_templates_dir(
    monkeypatch: pytest.MonkeyPatch,
    module,
    template_dir: Path,
) -> None:
    monkeypatch.setattr(module, "_templates_dir", lambda: template_dir)


def _bind_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
    module,
    *,
    storage_root: Path,
    agent_name: str = "codex",
    private: bool = False,
    settings: dict[str, object] | None = None,
):
    plugin_root = Path(module.__file__).resolve().parent
    runtime_paths = _FakeRuntimePaths(
        storage_root=storage_root,
        config_dir=plugin_root.parent,
    )
    config = _FakeConfig(
        agents={
            agent_name: _FakeAgentConfig(
                private=_FakePrivateConfig(per="user") if private else None,
            ),
        },
        plugins=[
            _FakePluginEntry(
                path=str(plugin_root),
                enabled=True,
                settings=settings or {},
            ),
        ],
    )
    ctx = _FakeRuntimeContext(
        agent_name=agent_name,
        room_id="!room:test",
        thread_id="$thread:test",
        resolved_thread_id="$thread:test",
        requester_id="@user:test",
        runtime_paths=runtime_paths,
        config=config,
    )
    monkeypatch.setattr(module, "get_tool_runtime_context", lambda: ctx)
    return ctx


def _todos_json_path(module, state_root: Path) -> Path:
    return module._todos_path(state_root, "!room:test", "$thread:test")


def test_render_basic_template(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "basic",
        """
        name: basic
        version: "1.0"
        description: Basic test template.
        todos:
          - title: "first"
          - title: "second"
            priority: high
            depends_on: [1]
        """,
    )

    rendered = module._render_template_definition("basic", {}, template_dir=template_dir)

    assert [item["title"] for item in rendered["todos"]] == ["first", "second"]
    assert rendered["todos"][1]["priority"] == "high"
    assert rendered["todos"][1]["depends_on"] == [1]


def test_pydantic_validates_required_params() -> None:
    module = _load_tools_module()
    schemas = module._TEMPLATE_SCHEMAS_MODULE

    with pytest.raises(ValidationError, match="REPO"):
        schemas.MindroomDevParams(ISSUE_REF="x")


def test_pydantic_validates_choices() -> None:
    module = _load_tools_module()
    schemas = module._TEMPLATE_SCHEMAS_MODULE

    with pytest.raises(ValidationError, match="REPO"):
        schemas.MindroomDevParams(ISSUE_REF="x", REPO="invalid")


def test_pydantic_validates_n_reviewers_range() -> None:
    module = _load_tools_module()
    schemas = module._TEMPLATE_SCHEMAS_MODULE

    with pytest.raises(ValidationError, match="N_REVIEWERS"):
        schemas.MindroomDevParams(ISSUE_REF="x", REPO="mindroom", N_REVIEWERS=0)


def test_todo_rejects_params_on_regular_todo() -> None:
    module = _load_tools_module()
    schemas = module._TEMPLATE_SCHEMAS_MODULE

    with pytest.raises(ValueError, match=r"Todo with `title` cannot use `params`"):
        schemas.Todo(title="x", params={"y": 1})


def test_todo_rejects_priority_on_sub_template() -> None:
    module = _load_tools_module()
    schemas = module._TEMPLATE_SCHEMAS_MODULE

    with pytest.raises(
        ValueError,
        match=r"Todo with `sub_template` cannot use `priority`",
    ):
        schemas.Todo(sub_template="x", priority="high")


def test_todo_rejects_assigned_agent_on_sub_template() -> None:
    module = _load_tools_module()
    schemas = module._TEMPLATE_SCHEMAS_MODULE

    with pytest.raises(
        ValueError,
        match=r"Todo with `sub_template` cannot use `assigned_agent`",
    ):
        schemas.Todo(sub_template="x", assigned_agent="codex")


def test_apply_template_missing_required_param_raises_value_error() -> None:
    module = _load_tools_module()

    with pytest.raises(
        ValueError,
        match=r"Invalid template 'mindroom-dev\.yaml\.j2': params validation failed: .*ISSUE_REF: Field required",
    ):
        module.WorkloopTodoManager().workloop_apply_template(None, "mindroom-dev", {})


def test_apply_template_invalid_choice_raises_value_error() -> None:
    module = _load_tools_module()

    with pytest.raises(
        ValueError,
        match=r"Invalid template 'mindroom-dev\.yaml\.j2': params validation failed: .*REPO: Input should be .*mindroom.*cinny.*nixos.*tuwunel",
    ):
        module.WorkloopTodoManager().workloop_apply_template(
            None,
            "mindroom-dev",
            {"ISSUE_REF": "x", "REPO": "invalid"},
        )


def test_apply_template_extra_param_raises_value_error() -> None:
    module = _load_tools_module()

    with pytest.raises(
        ValueError,
        match=r"Invalid template 'mindroom-dev\.yaml\.j2': params validation failed: .*EXTRA: Extra inputs are not permitted",
    ):
        module.WorkloopTodoManager().workloop_apply_template(
            None,
            "mindroom-dev",
            {"ISSUE_REF": "x", "REPO": "mindroom", "EXTRA": "junk"},
        )


def test_branch_defaults_from_issue_ref() -> None:
    module = _load_tools_module()

    rendered = module._render_template_definition(
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-201", "REPO": "mindroom"},
    )

    assert rendered["resolved_params"]["BRANCH"] == "issue-201"


def test_base_defaults_from_is_pr() -> None:
    module = _load_tools_module()

    pr_rendered = module._render_template_definition(
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-201", "REPO": "mindroom", "IS_PR": True},
    )
    local_rendered = module._render_template_definition(
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-201", "REPO": "mindroom", "IS_PR": False},
    )

    assert pr_rendered["resolved_params"]["BASE"] == "origin/main"
    assert local_rendered["resolved_params"]["BASE"] == "main"


def test_undefined_jinja_var_raises_with_filename(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "undefined-var",
        """
        name: undefined-var
        version: "1.0"
        description: Undefined var test.
        todos:
          - title: "{{ TYPO }}"
        """,
    )

    with pytest.raises(ValueError, match=r"undefined-var\.yaml\.j2"):
        module._render_template_definition("undefined-var", {}, template_dir=template_dir)


def test_malformed_jinja_syntax_fails_loud_on_apply(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "broken-jinja",
        """
        name: broken-jinja
        version: "1.0"
        description: Broken Jinja test.
        todos:
          - title: "{% if"
        """,
    )

    with pytest.raises(ValueError, match=r"broken-jinja\.yaml\.j2"):
        module._render_template_definition("broken-jinja", {}, template_dir=template_dir)


def test_unsafe_jinja_expression_fails_loud(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "unsafe-jinja",
        """
        name: unsafe-jinja
        version: "1.0"
        description: Unsafe Jinja test.
        todos:
          - title: "{{ cycler.__init__.__globals__.os.popen('id').read() }}"
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"unsafe-jinja\.yaml\.j2.*unsafe template expression",
    ):
        module._render_template_definition("unsafe-jinja", {}, template_dir=template_dir)


def test_sub_template_inlining(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "child",
        """
        name: child
        version: "1.0"
        description: Child template.
        todos:
          - title: "child one"
          - title: "child two"
            depends_on: [1]
        """,
    )
    _write_template(
        template_dir,
        "parent",
        """
        name: parent
        version: "1.0"
        description: Parent template.
        todos:
          - title: "start"
          - sub_template: child
            depends_on: [1]
          - title: "end"
            depends_on: [2]
        """,
    )

    rendered = module._render_template_definition("parent", {}, template_dir=template_dir)

    assert [item["title"] for item in rendered["todos"]] == [
        "start",
        "child one",
        "child two",
        "end",
    ]


def test_sub_template_dep_first_child(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "child",
        """
        name: child
        version: "1.0"
        description: Child template.
        todos:
          - title: "child one"
          - title: "child two"
            depends_on: [1]
        """,
    )
    _write_template(
        template_dir,
        "parent",
        """
        name: parent
        version: "1.0"
        description: Parent template.
        todos:
          - title: "start"
          - sub_template: child
            depends_on: [1]
        """,
    )

    rendered = module._render_template_definition("parent", {}, template_dir=template_dir)

    assert rendered["todos"][1]["depends_on"] == [1]


def test_sub_template_dep_last_child(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "child",
        """
        name: child
        version: "1.0"
        description: Child template.
        todos:
          - title: "child one"
          - title: "child two"
            depends_on: [1]
        """,
    )
    _write_template(
        template_dir,
        "parent",
        """
        name: parent
        version: "1.0"
        description: Parent template.
        todos:
          - title: "start"
          - sub_template: child
            depends_on: [1]
          - title: "end"
            depends_on: [2]
        """,
    )

    rendered = module._render_template_definition("parent", {}, template_dir=template_dir)

    assert rendered["todos"][3]["depends_on"] == [3]


def test_invalid_priority_in_template_fails_loud_on_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "bad-priority",
        """
        name: bad-priority
        version: "1.0"
        description: Bad priority.
        todos:
          - title: "bad"
            priority: urgent
        """,
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    _bind_scope(monkeypatch, module, tmp_path)

    with pytest.raises(ValueError, match=r"bad-priority\.yaml\.j2"):
        module.WorkloopTodoManager().workloop_apply_template(None, "bad-priority", {})


def test_out_of_range_depends_on_in_template_fails_loud_on_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "bad-dep",
        """
        name: bad-dep
        version: "1.0"
        description: Bad dep.
        todos:
          - title: "bad"
            depends_on: [99]
        """,
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    _bind_scope(monkeypatch, module, tmp_path)

    with pytest.raises(ValueError, match=r"bad-dep\.yaml\.j2"):
        module.WorkloopTodoManager().workloop_apply_template(None, "bad-dep", {})


def test_list_templates_returns_metadata_no_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "metadata-only",
        """
        name: metadata-only
        version: "1.0"
        description: Should list without rendering.
        todos:
          - title: "hello {{ MISSING_VALUE }}"
        """,
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()

    listed = manager.workloop_list_templates(None)

    assert "`metadata-only`" in listed
    with pytest.raises(ValueError, match=r"metadata-only\.yaml\.j2"):
        manager.workloop_apply_template(None, "metadata-only", {})


def test_no_schema_template_accepts_any_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "ad-hoc",
        """
        name: ad-hoc
        version: "1.0"
        description: Ad hoc template.
        todos:
          - title: "hello {{ anything }}"
        """,
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    _bind_scope(monkeypatch, module, tmp_path)

    preview = module.WorkloopTodoManager().workloop_apply_template(
        None,
        "ad-hoc",
        {"anything": "goes"},
        dry_run=True,
    )

    assert "hello goes" in preview


def test_dependency_cycle_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "cycle",
        """
        name: cycle
        version: "1.0"
        description: Cycle.
        todos:
          - title: "one"
            depends_on: [2]
          - title: "two"
            depends_on: [1]
        """,
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    _bind_scope(monkeypatch, module, tmp_path)

    with pytest.raises(ValueError, match="cycle"):
        module.WorkloopTodoManager().workloop_apply_template(None, "cycle", {})


def test_path_traversal_name_rejected() -> None:
    module = _load_tools_module()

    with pytest.raises(ValueError, match=r"invalid template name: '\.\./etc/passwd'"):
        module._render_template_definition("../etc/passwd", {})


def test_empty_sub_template_rejected(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "empty-child",
        """
        name: empty-child
        version: "1.0"
        description: Empty child.
        todos: []
        """,
    )
    _write_template(
        template_dir,
        "parent",
        """
        name: parent
        version: "1.0"
        description: Parent.
        todos:
          - sub_template: empty-child
        """,
    )

    with pytest.raises(ValueError, match=r"empty-child\.yaml\.j2"):
        module._render_template_definition("parent", {}, template_dir=template_dir)


def test_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tools_module()
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()
    todos_path = _todos_json_path(module, tmp_path)
    todos_path.parent.mkdir(parents=True, exist_ok=True)
    original_state = {
        "room_id": "!room:test",
        "thread_id": "$thread:test",
        "created_at": "2026-04-19T00:00:00+00:00",
        "updated_at": "2026-04-19T00:00:00+00:00",
        "items": [
            {
                "id": "aaaa1111",
                "title": "existing todo",
                "status": "open",
                "priority": "medium",
                "depends_on": [],
                "assigned_agent": "codex",
                "event_id": None,
                "created_at": "2026-04-19T00:00:00+00:00",
                "updated_at": "2026-04-19T00:00:00+00:00",
                "completed_at": None,
            }
        ],
    }
    todos_path.write_text(
        json.dumps(original_state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = todos_path.read_bytes()

    manager.workloop_apply_template(
        None,
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-TEST-159", "REPO": "mindroom"},
        dry_run=True,
    )

    assert todos_path.read_bytes() == before


def test_dry_run_returns_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tools_module()
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()

    preview = manager.workloop_apply_template(
        None,
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-TEST-159", "REPO": "mindroom"},
        dry_run=True,
    )

    assert "Template `mindroom-dev` v1.0" in preview
    assert "Spawn 2 planners" in preview
    assert "depends on 1" in preview


def test_apply_writes_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tools_module()
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()

    result = manager.workloop_apply_template(
        None,
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-TEST-159", "REPO": "mindroom"},
    )

    state = json.loads(_todos_json_path(module, tmp_path).read_text(encoding="utf-8"))

    assert "created 23 todo(s)" in result
    assert len(state["items"]) == 23


def test_apply_parallel_review_loop_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()

    result = manager.workloop_apply_template(
        None,
        "parallel-review-loop",
        {"N_REVIEWERS": 8},
    )

    state = json.loads(_todos_json_path(module, tmp_path).read_text(encoding="utf-8"))
    items = state["items"]

    assert "created 9 todo(s)" in result
    assert len(items) == 9
    assert items[0]["title"].startswith("Archive or remove stale REVIEW-*.md")
    assert items[0]["depends_on"] == []
    assert items[1]["depends_on"] == [items[0]["id"]]
    assert items[2]["depends_on"] == [items[1]["id"]]
    assert items[3]["depends_on"] == [items[2]["id"]]
    assert items[4]["depends_on"] == [items[3]["id"]]
    assert items[5]["depends_on"] == [items[4]["id"]]
    assert items[6]["depends_on"] == [items[5]["id"]]
    assert items[7]["depends_on"] == [items[6]["id"]]
    assert items[8]["depends_on"] == [items[7]["id"]]
    assert "Render canonical reviewer prompts" in items[1]["title"]
    assert "Audit generated reviewer prompts" in items[2]["title"]
    assert "--letters a,b,c,d,e,f,g,h" in items[2]["title"]
    assert "N=8 reviewers" in items[3]["title"]
    assert "audited canonical prompts" in items[3]["title"]

    with pytest.raises(
        ValueError, match=r"N_REVIEWERS: Input should be greater than or equal to 1"
    ):
        manager.workloop_apply_template(
            None,
            "parallel-review-loop",
            {"N_REVIEWERS": 0},
        )


def test_apply_template_with_missing_sub_template_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "parent",
        """
        name: parent
        version: "1.0"
        description: Parent template.
        todos:
          - sub_template: missing-child
        """,
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    _bind_scope(monkeypatch, module, tmp_path)

    with pytest.raises(ValueError, match="missing-child"):
        module.WorkloopTodoManager().workloop_apply_template(None, "parent", {})


def test_dependency_chain_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()
    manager.workloop_apply_template(
        None,
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-TEST-159", "REPO": "mindroom"},
    )

    state = json.loads(_todos_json_path(module, tmp_path).read_text(encoding="utf-8"))
    items_by_id = {item["id"]: item for item in state["items"]}
    leaf = next(
        item
        for item in state["items"]
        if item["title"] == "Post final thread summary with recap footer (gate: message sent)"
    )

    seen_titles: set[str] = set()

    def walk(todo_id: str) -> None:
        item = items_by_id[todo_id]
        seen_titles.add(item["title"])
        for dep_id in item["depends_on"]:
            assert dep_id in items_by_id
            walk(dep_id)

    walk(leaf["id"])

    assert any(
        title.startswith("Create living report at skills/mindroom-dev")
        for title in seen_titles
    )


def test_mindroom_dev_cinny_deploy_on_critical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    _bind_scope(monkeypatch, module, tmp_path)
    manager = module.WorkloopTodoManager()
    manager.workloop_apply_template(
        None,
        "mindroom-dev",
        {"ISSUE_REF": "ISSUE-TEST-159", "REPO": "cinny"},
    )

    state = json.loads(_todos_json_path(module, tmp_path).read_text(encoding="utf-8"))
    items_by_id = {item["id"]: item for item in state["items"]}
    leaf = next(
        item
        for item in state["items"]
        if item["title"] == "Post final thread summary with recap footer (gate: message sent)"
    )

    seen_titles: set[str] = set()

    def walk(todo_id: str) -> None:
        item = items_by_id[todo_id]
        seen_titles.add(item["title"])
        for dep_id in item["depends_on"]:
            walk(dep_id)

    walk(leaf["id"])

    assert any(title.startswith("Cinny deploy: npm run build") for title in seen_titles)


def test_parallel_review_loop_unanimous_approve_path_completes() -> None:
    module = _load_tools_module()

    rendered = module._render_template_definition("parallel-review-loop", {})

    assert "mark complete immediately as no-op" in rendered["todos"][7]["title"]


def test_parallel_review_loop_has_render_audit_gate_before_spawn() -> None:
    module = _load_tools_module()

    rendered = module._render_template_definition(
        "parallel-review-loop", {"N_REVIEWERS": 8}
    )
    todos = rendered["todos"]

    assert todos[0]["title"].startswith("Archive or remove stale REVIEW-*.md")
    assert "prior-round REVIEW files" in todos[0]["title"]
    assert todos[1]["title"].startswith("Render canonical reviewer prompts")
    assert "prompt-templates.md" in todos[1]["title"]
    assert todos[2]["title"].startswith("Audit generated reviewer prompts")
    assert "--letters a,b,c,d,e,f,g,h" in todos[2]["title"]
    assert "clean completeness audit before reviewer spawn" in todos[2]["title"]
    assert todos[3]["title"].startswith("Spawn N=8 reviewers")
    assert todos[3]["depends_on"] == [3]


def test_parallel_review_loop_respawn_kills_all_and_reruns_gate() -> None:
    module = _load_tools_module()

    rendered = module._render_template_definition("parallel-review-loop", {})
    respawn_title = rendered["todos"][8]["title"]

    assert "kill every reviewer tmux session" in respawn_title
    assert "archive or remove stale REVIEW-*.md files" in respawn_title
    assert "rerun render plus audit" in respawn_title
    assert "--letters a,b,c,d,e,f,g,h" in respawn_title
    assert "before any brand-new reviewer starts" in respawn_title


def test_parallel_review_loop_template_removes_stale_reviewer_context_instructions() -> None:
    module = _load_tools_module()

    rendered = module._render_template_definition("parallel-review-loop", {})
    titles = "\n".join(todo["title"] for todo in rendered["todos"])

    assert "prior approvers get" not in titles
    assert "/new" not in titles
    assert "change-requesters keep context" not in titles
    assert "targeted recheck" not in titles
    assert "previous-round context" not in titles


def test_recursion_depth_cap(tmp_path: Path) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    _write_template(
        template_dir,
        "d",
        """
        name: d
        version: "1.0"
        description: D.
        todos:
          - title: "deepest"
        """,
    )
    _write_template(
        template_dir,
        "c",
        """
        name: c
        version: "1.0"
        description: C.
        todos:
          - sub_template: d
        """,
    )
    _write_template(
        template_dir,
        "b",
        """
        name: b
        version: "1.0"
        description: B.
        todos:
          - sub_template: c
        """,
    )
    _write_template(
        template_dir,
        "a",
        """
        name: a
        version: "1.0"
        description: A.
        todos:
          - sub_template: b
        """,
    )

    with pytest.raises(ValueError, match="Template recursion depth exceeded"):
        module._render_template_definition("a", {}, template_dir=template_dir)


def test_unknown_template_raises() -> None:
    module = _load_tools_module()

    with pytest.raises(ValueError, match="Unknown template: 'missing-template'"):
        module._render_template_definition("missing-template", {})


def test_list_templates_includes_json_schema_when_registered() -> None:
    module = _load_tools_module()
    listed = module.WorkloopTodoManager().workloop_list_templates(None)

    assert "| source | name | version | description | json schema |" in listed
    assert '"REPO"' in listed
    assert '"enum": ["mindroom", "cinny", "nixos", "tuwunel"]' in listed
    assert '"default": 8' in listed
    assert '"minimum": 1' in listed


def test_list_templates_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    external_dir = tmp_path / "external"
    template_dir.mkdir(parents=True, exist_ok=True)
    external_dir.mkdir(parents=True, exist_ok=True)
    external_template = external_dir / "escape.yaml.j2"
    external_template.write_text(
        'name: escape\nversion: "1.0"\ndescription: escaped\ntodos:\n  - title: "escaped"\n',
        encoding="utf-8",
    )
    (template_dir / "escape.yaml.j2").symlink_to(external_template)
    _bind_templates_dir(monkeypatch, module, template_dir)

    with pytest.raises(ValueError, match="escapes templates dir"):
        module.WorkloopTodoManager().workloop_list_templates(None)


def test_malformed_template_fails_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "broken.yaml.j2").write_text(
        'name: broken\nversion: "1.0"\ndescription: broken\ntodos:\n  - title: "oops"\n    depends_on: [\n',
        encoding="utf-8",
    )
    _bind_templates_dir(monkeypatch, module, template_dir)
    manager = module.WorkloopTodoManager()

    with pytest.raises(ValueError, match=r"broken\.yaml\.j2"):
        manager.workloop_list_templates(None)


def test_workspace_template_overrides_builtin_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    builtin_dir = tmp_path / "builtin-templates"
    workspace_template_dir = (
        tmp_path
        / "storage"
        / "agents"
        / "codex"
        / "workspace"
        / "workloop"
        / "templates"
    )
    _write_template(
        builtin_dir,
        "override",
        """
        name: override
        version: "1.0"
        description: Builtin version.
        todos:
          - title: "builtin task"
        """,
    )
    _write_template(
        workspace_template_dir,
        "override",
        """
        name: override
        version: "1.0"
        description: Workspace version.
        todos:
          - title: "workspace task"
        """,
    )
    _bind_templates_dir(monkeypatch, module, builtin_dir)
    _bind_runtime_context(monkeypatch, module, storage_root=tmp_path / "storage")

    preview = module.WorkloopTodoManager().workloop_apply_template(
        None,
        "override",
        {},
        dry_run=True,
    )

    assert "workspace task" in preview
    assert "builtin task" not in preview


def test_list_templates_prefers_workspace_template_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    builtin_dir = tmp_path / "builtin-templates"
    workspace_template_dir = (
        tmp_path
        / "storage"
        / "agents"
        / "codex"
        / "workspace"
        / "workloop"
        / "templates"
    )
    _write_template(
        builtin_dir,
        "shared-name",
        """
        name: shared-name
        version: "1.0"
        description: Builtin version.
        todos:
          - title: "builtin task"
        """,
    )
    _write_template(
        workspace_template_dir,
        "shared-name",
        """
        name: shared-name
        version: "2.0"
        description: Workspace version.
        todos:
          - title: "workspace task"
        """,
    )
    _write_template(
        workspace_template_dir,
        "workspace-only",
        """
        name: workspace-only
        version: "1.0"
        description: Workspace only.
        todos:
          - title: "workspace-only task"
        """,
    )
    _bind_templates_dir(monkeypatch, module, builtin_dir)
    _bind_runtime_context(monkeypatch, module, storage_root=tmp_path / "storage")

    listed = module.WorkloopTodoManager().workloop_list_templates(None)

    assert "| source | name | version | description | json schema |" in listed
    assert listed.count("`shared-name`") == 1
    assert "| `workspace` | `shared-name` | `2.0` | Workspace version." in listed
    assert "`workspace-only`" in listed


def test_apply_workspace_template_writes_todos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    workspace_template_dir = (
        tmp_path
        / "storage"
        / "agents"
        / "codex"
        / "workspace"
        / "workloop"
        / "templates"
    )
    _write_template(
        workspace_template_dir,
        "workspace-plan",
        """
        name: workspace-plan
        version: "1.0"
        description: Workspace plan.
        todos:
          - title: "first workspace task"
          - title: "second workspace task"
            depends_on: [1]
        """,
    )
    _bind_templates_dir(monkeypatch, module, tmp_path / "empty-builtins")
    _bind_runtime_context(monkeypatch, module, storage_root=tmp_path / "storage")

    result = module.WorkloopTodoManager().workloop_apply_template(
        None,
        "workspace-plan",
        {},
    )
    state_path = _todos_json_path(module, tmp_path / "storage" / "plugins" / "workloop")
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert "created 2 todo(s)" in result
    assert [item["title"] for item in state["items"]] == [
        "first workspace task",
        "second workspace task",
    ]
    assert state["items"][1]["depends_on"] == [state["items"][0]["id"]]


def test_workspace_template_can_expand_builtin_sub_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    builtin_dir = tmp_path / "builtin-templates"
    workspace_template_dir = (
        tmp_path
        / "storage"
        / "agents"
        / "codex"
        / "workspace"
        / "workloop"
        / "templates"
    )
    _write_template(
        builtin_dir,
        "builtin-child",
        """
        name: builtin-child
        version: "1.0"
        description: Builtin child.
        todos:
          - title: "child one"
          - title: "child two"
            depends_on: [1]
        """,
    )
    _write_template(
        workspace_template_dir,
        "workspace-parent",
        """
        name: workspace-parent
        version: "1.0"
        description: Workspace parent.
        todos:
          - title: "parent start"
          - sub_template: builtin-child
            depends_on: [1]
        """,
    )
    _bind_templates_dir(monkeypatch, module, builtin_dir)
    _bind_runtime_context(monkeypatch, module, storage_root=tmp_path / "storage")

    preview = module.WorkloopTodoManager().workloop_apply_template(
        None,
        "workspace-parent",
        {},
        dry_run=True,
    )

    assert "parent start" in preview
    assert "child one" in preview
    assert "child two" in preview


def test_include_builtin_templates_false_hides_builtin_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    builtin_dir = tmp_path / "builtin-templates"
    _write_template(
        builtin_dir,
        "builtin-only",
        """
        name: builtin-only
        version: "1.0"
        description: Builtin only.
        todos:
          - title: "builtin task"
        """,
    )
    _bind_templates_dir(monkeypatch, module, builtin_dir)
    _bind_runtime_context(
        monkeypatch,
        module,
        storage_root=tmp_path / "storage",
        settings={"include_builtin_templates": False},
    )

    listed = module.WorkloopTodoManager().workloop_list_templates(None)

    assert "`builtin-only`" not in listed
    with pytest.raises(ValueError, match="Unknown template: 'builtin-only'"):
        module.WorkloopTodoManager().workloop_apply_template(
            None,
            "builtin-only",
            {},
            dry_run=True,
        )


def test_workspace_templates_use_private_resolved_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    private_workspace = tmp_path / "private-state" / "codex_data"
    workspace_template_dir = private_workspace / "workloop" / "templates"
    _write_template(
        workspace_template_dir,
        "private-template",
        """
        name: private-template
        version: "1.0"
        description: Private template.
        todos:
          - title: "private workspace task"
        """,
    )
    ctx = _bind_runtime_context(
        monkeypatch,
        module,
        storage_root=tmp_path / "storage",
        private=True,
    )
    monkeypatch.setattr(
        module,
        "build_execution_identity_from_runtime_context",
        lambda runtime_context: _FakeExecutionIdentity(
            agent_name=runtime_context.agent_name,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "resolve_agent_runtime",
        lambda agent_name, config, runtime_paths, execution_identity, create=False: _FakeAgentRuntime(
            workspace=_FakeWorkspace(root=private_workspace),
        ),
        raising=False,
    )

    preview = module.WorkloopTodoManager().workloop_apply_template(
        None,
        "private-template",
        {},
        dry_run=True,
    )

    assert ctx.agent_name == "codex"
    assert "private workspace task" in preview


def test_list_templates_rejects_workspace_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tools_module()
    workspace_template_dir = (
        tmp_path
        / "storage"
        / "agents"
        / "codex"
        / "workspace"
        / "workloop"
        / "templates"
    )
    external_dir = tmp_path / "external"
    workspace_template_dir.mkdir(parents=True, exist_ok=True)
    external_dir.mkdir(parents=True, exist_ok=True)
    external_template = external_dir / "escape.yaml.j2"
    external_template.write_text(
        'name: escape\nversion: "1.0"\ndescription: escaped\ntodos:\n  - title: "escaped"\n',
        encoding="utf-8",
    )
    (workspace_template_dir / "escape.yaml.j2").symlink_to(external_template)
    _bind_templates_dir(monkeypatch, module, tmp_path / "empty-builtins")
    _bind_runtime_context(monkeypatch, module, storage_root=tmp_path / "storage")

    with pytest.raises(ValueError, match="escapes templates dir"):
        module.WorkloopTodoManager().workloop_list_templates(None)
