"""Agent-facing tools for the MindRoom workloop plugin.

This file keeps the runtime logic and helpers local, and dynamically loads the
sibling ``template_schemas.py`` module so plugin imports remain reliable under
MindRoom's ``spec_from_file_location`` loader.

Provides a ``WorkloopTodoManager`` toolkit that agents can use to create work plans,
add/complete/update/list per-thread todos with dependencies.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import util as importlib_util
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit
from agno.agent import Agent
from agno.team.team import Team
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from pydantic import ValidationError
import yaml

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_plugin_state_root,
    get_tool_runtime_context,
)
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import agent_workspace_root_path

# Runtime imports needed for Agno toolkit introspection.
if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = logging.getLogger(__name__)

_PLUGIN_NAME = "workloop"

# ══════════════════════════════════════════════════════════════════════
# Constants (duplicated from hooks.py — self-contained requirement)
# ══════════════════════════════════════════════════════════════════════

VALID_PRIORITIES = {"low", "medium", "high", "critical"}
TERMINAL_STATUSES = {"done", "cancelled"}
PRIORITY_EMOJI: dict[str, str] = {
    "critical": "\U0001f534",
    "high": "\U0001f7e0",
    "medium": "\U0001f7e1",
    "low": "\U0001f7e2",
}
PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
TEMPLATE_RECURSION_LIMIT = 3
WORKSPACE_TEMPLATE_RELATIVE_DIR = Path("workloop/templates")
_JINJA_ENV = Environment(autoescape=False, undefined=StrictUndefined)


@dataclass(frozen=True, slots=True)
class TemplateRoot:
    path: Path
    source: str


def _load_template_schemas_module() -> Any:
    module_name = f"{__name__}_template_schemas"
    module = sys.modules.get(module_name)
    if module is not None:
        return module

    path = Path(__file__).with_name("template_schemas.py")
    spec = importlib_util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load template schemas from {path}")

    module = importlib_util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_TEMPLATE_SCHEMAS_MODULE = _load_template_schemas_module()
PARAMS_SCHEMAS = _TEMPLATE_SCHEMAS_MODULE.PARAMS_SCHEMAS
TemplateDocument = _TEMPLATE_SCHEMAS_MODULE.TemplateDocument

# ══════════════════════════════════════════════════════════════════════
# Helpers (duplicated from hooks.py — self-contained requirement)
# ══════════════════════════════════════════════════════════════════════


def _sanitize(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", value)).strip("_")


def _thread_key(room_id: str, thread_id: str | None) -> str:
    resolved = thread_id or "main"
    return f"{_sanitize(room_id)}_{_sanitize(resolved)}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _short_id(existing_ids: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing_ids:
            return candidate


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_update_json(path: Path, mutate: Any) -> Any:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
            result = mutate(data)
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _todos_path(state_root: Path, room_id: str, thread_id: str | None) -> Path:
    key = _thread_key(room_id, thread_id)
    return state_root / "threads" / key / "todos.json"


def _ensure_thread_state(
    data: dict[str, Any], room_id: str, thread_id: str | None
) -> None:
    resolved = thread_id or "main"
    if "items" not in data:
        data["room_id"] = room_id
        data["thread_id"] = resolved
        data["created_at"] = _now_iso()
        data["updated_at"] = _now_iso()
        data["items"] = []


def is_blocked(item: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    for dep_id in item.get("depends_on", []):
        dep = items_by_id.get(dep_id)
        if dep is None:
            continue
        if dep["status"] not in TERMINAL_STATUSES:
            return True
    return False


def is_actionable(item: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    return item["status"] == "open" and not is_blocked(item, items_by_id)


def _would_create_cycle(
    items_by_id: dict[str, dict[str, Any]], item_id: str, new_dep_id: str
) -> bool:
    stack = [new_dep_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == item_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        current_item = items_by_id.get(current)
        if current_item is not None:
            stack.extend(current_item.get("depends_on", []))
    return False


def _newly_unblocked(
    items: list[dict[str, Any]], changed_id: str
) -> list[dict[str, Any]]:
    items_by_id = {item["id"]: item for item in items}
    unblocked: list[dict[str, Any]] = []
    for item in items:
        if item["status"] != "open":
            continue
        if changed_id not in item.get("depends_on", []):
            continue
        if is_actionable(item, items_by_id):
            unblocked.append(item)
    return unblocked


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def _validate_template_name(name: str) -> None:
    if (
        not name
        or "/" in name
        or "\\" in name
        or ".." in name
        or Path(name).is_absolute()
    ):
        raise ValueError(f"invalid template name: '{name}'")


def _template_path(name: str, template_dir: Path | None = None) -> Path:
    _validate_template_name(name)
    root = (template_dir or _templates_dir()).resolve()
    path = (root / f"{name}.yaml.j2").resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"invalid template name: '{name}'")
    return path


def _configured_plugin_settings() -> dict[str, Any]:
    """Return this plugin's config settings from the active tool context."""
    ctx = get_tool_runtime_context()
    if ctx is None or getattr(ctx, "config", None) is None:
        return {}

    plugin_root = Path(__file__).resolve().parent
    runtime_paths = ctx.runtime_paths
    for plugin_entry in getattr(ctx.config, "plugins", ()) or ():
        if not getattr(plugin_entry, "enabled", True):
            continue
        plugin_path = getattr(plugin_entry, "path", "")
        try:
            configured_root = Path(plugin_path).expanduser()
            if not configured_root.is_absolute():
                config_dir = getattr(runtime_paths, "config_dir", None)
                if config_dir is None:
                    continue
                configured_root = Path(config_dir) / configured_root
            if configured_root.resolve() != plugin_root:
                continue
        except OSError:
            continue
        return dict(getattr(plugin_entry, "settings", {}) or {})
    return {}


def _include_builtin_templates(settings: Mapping[str, Any] | None = None) -> bool:
    """Return whether bundled plugin templates should be visible."""
    resolved_settings = _configured_plugin_settings() if settings is None else settings
    return resolved_settings.get("include_builtin_templates") is not False


def _current_agent_workspace_root() -> Path | None:
    """Return the current agent workspace root for shared and private agents."""
    ctx = get_tool_runtime_context()
    if ctx is None or getattr(ctx, "config", None) is None:
        return None

    agent_config = (getattr(ctx.config, "agents", {}) or {}).get(ctx.agent_name)
    if agent_config is None:
        return None

    if getattr(agent_config, "private", None) is not None:
        execution_identity = build_execution_identity_from_runtime_context(ctx)
        agent_runtime = resolve_agent_runtime(
            ctx.agent_name,
            ctx.config,
            ctx.runtime_paths,
            execution_identity=execution_identity,
            create=True,
        )
        workspace = getattr(agent_runtime, "workspace", None)
        return workspace.root if workspace is not None else None

    return agent_workspace_root_path(ctx.runtime_paths.storage_root, ctx.agent_name)


def _visible_template_roots(
    settings: Mapping[str, Any] | None = None,
) -> tuple[TemplateRoot, ...]:
    roots: list[TemplateRoot] = []
    workspace_root = _current_agent_workspace_root()
    if workspace_root is not None:
        roots.append(
            TemplateRoot(
                path=workspace_root / WORKSPACE_TEMPLATE_RELATIVE_DIR,
                source="workspace",
            )
        )
    if _include_builtin_templates(settings):
        roots.append(TemplateRoot(path=_templates_dir(), source="builtin"))
    return tuple(roots)


def _template_roots_from_dir(template_dir: Path | None) -> tuple[TemplateRoot, ...]:
    return (TemplateRoot(path=template_dir or _templates_dir(), source="builtin"),)


def _resolve_template_path(
    name: str,
    template_roots: Sequence[TemplateRoot],
) -> tuple[Path, TemplateRoot]:
    _validate_template_name(name)
    for template_root in template_roots:
        path = _template_path(name, template_root.path)
        if path.is_file():
            return path, template_root
    raise ValueError(f"Unknown template: '{name}'")


def _render_jinja_template(
    template_text: str,
    params: Mapping[str, Any],
    *,
    path: Path,
) -> str:
    try:
        return _JINJA_ENV.from_string(template_text).render(**params)
    except UndefinedError as exc:
        raise _template_value_error(path, f"undefined variable: {exc}") from exc
    except TemplateSyntaxError as exc:
        raise _template_value_error(path, f"syntax error: {exc}") from exc


def _template_value_error(path: Path, message: str) -> ValueError:
    return ValueError(f"Invalid template '{path.name}': {message}")


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _load_template_document(path: Path, text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _template_value_error(path, str(exc)) from exc
    if not isinstance(document, dict):
        raise _template_value_error(path, "top level must be a mapping")
    return document


def _validate_template_document(
    template: Mapping[str, Any],
    path: Path,
) -> Any:
    try:
        document = TemplateDocument.model_validate(template)
    except ValidationError as exc:
        raise _template_value_error(
            path,
            f"document validation failed: {_format_validation_error(exc)}",
        ) from exc
    expected_name = path.name.removesuffix(".yaml.j2")
    if document.name != expected_name:
        raise _template_value_error(
            path,
            f"name must match filename stem '{expected_name}'",
        )
    return document


def _validate_dependency_cycle(template_name: str, todos: list[dict[str, Any]]) -> None:
    states: dict[int, int] = {}
    stack: list[int] = []

    def visit(node_id: int) -> None:
        state = states.get(node_id, 0)
        if state == 1:
            cycle_start = stack.index(node_id)
            cycle = stack[cycle_start:] + [node_id]
            cycle_str = ", ".join(str(node) for node in cycle)
            raise ValueError(
                f"template '{template_name}' has dependency cycle: {cycle_str}"
            )
        if state == 2:
            return
        states[node_id] = 1
        stack.append(node_id)
        for dep_id in todos[node_id - 1].get("depends_on", []):
            visit(dep_id)
        stack.pop()
        states[node_id] = 2

    for node_id in range(1, len(todos) + 1):
        visit(node_id)


def _load_template_metadata(path: Path) -> dict[str, str]:
    template = _load_template_document(path, path.read_text(encoding="utf-8"))
    expected_name = path.name.removesuffix(".yaml.j2")
    name = template.get("name")
    version = template.get("version")
    description = template.get("description")
    if not isinstance(name, str) or name != expected_name:
        raise _template_value_error(
            path,
            f"name must match filename stem '{expected_name}'",
        )
    if not isinstance(version, str):
        raise _template_value_error(path, "version must be a string")
    if not isinstance(description, str):
        raise _template_value_error(path, "description must be a string")
    return {"name": name, "version": version, "description": description}


def _validate_depends_on_indexes(
    todos: list[dict[str, Any]],
    *,
    path: Path,
) -> None:
    total_items = len(todos)
    for entry in todos:
        for dep in entry.get("depends_on", []):
            if dep < 1 or dep > total_items:
                raise _template_value_error(
                    path,
                    f"depends_on index {dep} is out of range 1..{total_items}",
                )


def _render_template_definition(
    name: str,
    params: dict[str, Any],
    *,
    template_dir: Path | None = None,
    template_roots: Sequence[TemplateRoot] | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    if depth > TEMPLATE_RECURSION_LIMIT:
        raise ValueError(
            f"Template recursion depth exceeded while expanding '{name}'"
        )

    resolved_template_roots = (
        tuple(template_roots)
        if template_roots is not None
        else _template_roots_from_dir(template_dir)
    )
    path, _template_root = _resolve_template_path(name, resolved_template_roots)

    raw_text = path.read_text(encoding="utf-8")
    raw_template = _load_template_document(path, raw_text)
    _validate_template_document(raw_template, path)
    schema = PARAMS_SCHEMAS.get(name)
    if schema is None:
        resolved_params = dict(params)
    else:
        try:
            resolved_params = schema.model_validate(params).model_dump(mode="python")
        except ValidationError as exc:
            raise _template_value_error(
                path,
                f"params validation failed: {_format_validation_error(exc)}",
            ) from exc

    rendered_text = _render_jinja_template(raw_text, resolved_params, path=path)
    rendered_template = _load_template_document(path, rendered_text)
    rendered_document = _validate_template_document(rendered_template, path)
    rendered_todos = rendered_document.model_dump(mode="python", exclude_none=True)["todos"]
    _validate_depends_on_indexes(rendered_todos, path=path)
    expanded_todos = _expand_template_todos(
        rendered_todos,
        template_dir=template_dir,
        template_roots=resolved_template_roots,
        depth=depth,
    )
    _validate_dependency_cycle(rendered_document.name, expanded_todos)

    return {
        "name": rendered_document.name,
        "version": rendered_document.version,
        "description": rendered_document.description,
        "resolved_params": resolved_params,
        "todos": expanded_todos,
    }


def _expand_template_todos(
    todos: list[dict[str, Any]],
    *,
    template_dir: Path | None = None,
    template_roots: Sequence[TemplateRoot] | None = None,
    depth: int,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    index_map: dict[int, tuple[int, int]] = {}

    for original_index, entry in enumerate(todos, start=1):
        if entry.get("title") is not None:
            expanded.append(
                {
                    "title": entry["title"],
                    "priority": entry.get("priority", "medium"),
                    "assigned_agent": entry.get("assigned_agent", ""),
                    "depends_on": [],
                    "_parent_depends_on": list(entry.get("depends_on", [])),
                }
            )
            flat_index = len(expanded)
            index_map[original_index] = (flat_index, flat_index)
            continue

        child_template = _render_template_definition(
            entry["sub_template"],
            entry.get("params", {}),
            template_dir=template_dir,
            template_roots=template_roots,
            depth=depth + 1,
        )
        offset = len(expanded)
        for child in child_template["todos"]:
            expanded.append(
                {
                    "title": child["title"],
                    "priority": child.get("priority", "medium"),
                    "assigned_agent": child.get("assigned_agent", ""),
                    "depends_on": [dep + offset for dep in child.get("depends_on", [])],
                    "_parent_depends_on": [],
                }
            )
        first_child = offset + 1
        last_child = len(expanded)
        index_map[original_index] = (first_child, last_child)
        if entry.get("depends_on"):
            expanded[first_child - 1]["_parent_depends_on"].extend(entry["depends_on"])

    for item in expanded:
        item["depends_on"].extend(
            index_map[dep][1] for dep in item.pop("_parent_depends_on")
        )

    return expanded


def _format_param_value(value: Any) -> str:
    if isinstance(value, str):
        return f"`{value}`"
    return f"`{value}`"


def _format_template_preview(
    template_name: str,
    version: str,
    resolved_params: Mapping[str, Any],
    todos: list[dict[str, Any]],
) -> str:
    lines = [f"Template `{template_name}` v{version}", ""]
    if resolved_params:
        lines.append("Resolved params:")
        for key, value in resolved_params.items():
            lines.append(f"- `{key}`: {_format_param_value(value)}")
        lines.append("")
    lines.append("Preview:")
    for index, item in enumerate(todos, start=1):
        deps = item.get("depends_on", [])
        dep_suffix = f" (depends on {', '.join(str(dep) for dep in deps)})" if deps else ""
        lines.append(
            f"- {index}. [{item.get('priority', 'medium')}] {item['title']}{dep_suffix}"
        )
    return "\n".join(lines)


def _format_template_apply_result(
    template_name: str,
    version: str,
    resolved_params: Mapping[str, Any],
    created_items: list[dict[str, Any]],
) -> str:
    lines = [
        f"Applied template `{template_name}` v{version}: created {len(created_items)} todo(s).",
        "",
    ]
    if resolved_params:
        lines.append("Resolved params:")
        for key, value in resolved_params.items():
            lines.append(f"- `{key}`: {_format_param_value(value)}")
        lines.append("")
    lines.append("Created:")
    for item in created_items:
        lines.append(f"- `{item['id']}` {item['title']}")
    return "\n".join(lines)


def _format_templates_table(templates: list[dict[str, Any]]) -> str:
    lines = [
        "| source | name | version | description | json schema |",
        "| --- | --- | --- | --- | --- |",
    ]
    for template in templates:
        json_schema = template["json_schema"] or "-"
        lines.append(
            f"| `{template['source']}` | `{template['name']}` | `{template['version']}` | {template['description']} | {json_schema} |"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Thread resolution via tool runtime context
# ══════════════════════════════════════════════════════════════════════


def _current_scope(runtime_paths: RuntimePaths | None) -> tuple[Path, str, str, str]:
    """Return (state_root, room_id, thread_id, agent_name) from runtime context."""
    ctx = get_tool_runtime_context()
    if ctx is None:
        msg = "workloop_todo_manager requires an active tool runtime context"
        raise RuntimeError(msg)
    state_root = get_plugin_state_root(_PLUGIN_NAME, runtime_paths=ctx.runtime_paths)
    # Agent tool calls must follow the actual response thread target so the
    # persisted work state lines up with later enrichment and auto-pokes.
    thread_id = ctx.resolved_thread_id or ctx.thread_id or "main"
    return state_root, ctx.room_id, thread_id, ctx.agent_name


def _configured_agent_names() -> set[str]:
    """Return the set of configured agent names from the runtime context."""
    ctx = get_tool_runtime_context()
    if ctx is None or ctx.config is None:
        return set()
    return set((ctx.config.agents or {}).keys())


# ══════════════════════════════════════════════════════════════════════
# Toolkit
# ══════════════════════════════════════════════════════════════════════


class WorkloopTodoManager(Toolkit):
    """Toolkit for managing per-thread work plans with dependencies.

    All operations are scoped to the current thread (or room-level if not
    in a thread). State is persisted in JSON files under the plugin state root.
    """

    def __init__(self, runtime_paths: object | None = None) -> None:
        self._runtime_paths = runtime_paths
        super().__init__(
            name="workloop_todo_manager",
            instructions=(
                "Use these tools to manage a per-thread work plan with dependencies. "
                "You can create plans, add individual tasks, complete them, and update "
                "priorities, assignments, and dependencies. Items are scoped to the "
                "current conversation thread. Use `plan` for multi-step work and "
                "`complete_todo` as you finish each item."
            ),
            tools=[
                self.plan,
                self.add_todo,
                self.complete_todo,
                self.list_todos,
                self.update_todo,
                self.workloop_apply_template,
                self.workloop_list_templates,
            ],
        )

    def plan(
        self,
        agent: Agent | Team,
        tasks: str,
    ) -> str:
        """Create a multi-step work plan for the current thread.

        Creates one todo item per non-empty line. Lines can optionally start
        with a ``[priority]`` prefix (e.g. ``[high] Implement auth``).
        All items are assigned to the calling agent by default.

        Args:
            agent: The calling agent (injected automatically).
            tasks: Multi-line string with one task per line.

        Returns:
            Confirmation with all created item IDs.

        """
        state_root, room_id, thread_id, agent_name = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        lines = [line.strip() for line in tasks.strip().splitlines() if line.strip()]
        if not lines:
            return "No tasks provided. Write one task per line."

        parsed: list[tuple[str, str]] = []
        for line in lines:
            priority = "medium"
            title = line
            # Strip leading list markers like "1.", "2.", "-", "*"
            title = re.sub(r"^(\d+[\.\)]\s*|[-*]\s+)", "", title).strip()
            for p in VALID_PRIORITIES:
                prefix = f"[{p}] "
                if title.lower().startswith(prefix):
                    priority = p
                    title = title[len(prefix) :]
                    break
            if title:
                parsed.append((title, priority))

        if not parsed:
            return "No valid tasks found after parsing."

        def create_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
            _ensure_thread_state(data, room_id, thread_id)
            existing_ids = {i["id"] for i in data["items"]}
            created: list[dict[str, Any]] = []
            now = _now_iso()
            for title, priority in parsed:
                new_id = _short_id(existing_ids)
                existing_ids.add(new_id)
                item = {
                    "id": new_id,
                    "title": title,
                    "status": "open",
                    "priority": priority,
                    "depends_on": [],
                    "assigned_agent": agent_name,
                    "event_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                data["items"].append(item)
                created.append(item)
            data["updated_at"] = now
            return created

        created = _locked_update_json(path, create_plan)

        result_lines = [f"Created {len(created)} item(s) in thread work plan:\n"]
        for item in created:
            emoji = PRIORITY_EMOJI.get(item["priority"], "")
            result_lines.append(
                f"- {emoji} `{item['id']}` {item['title']} [{item['priority']}]"
            )
        return "\n".join(result_lines)

    def add_todo(
        self,
        agent: Agent | Team,
        title: str,
        depends_on: str = "",
        priority: str = "medium",
        assigned_agent: str = "",
    ) -> str:
        """Add a single todo item to the current thread's work plan.

        Args:
            agent: The calling agent (injected automatically).
            title: Title or summary of the todo.
            depends_on: Comma-separated IDs of items this depends on.
            priority: Priority level: low, medium, high, or critical.
            assigned_agent: Agent name to assign to (defaults to calling agent).

        Returns:
            Confirmation message with the new todo's ID.

        """
        state_root, room_id, thread_id, agent_name = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        priority = priority.lower()
        if priority not in VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. Must be: low, medium, high, critical."
            )

        dep_ids = (
            [d.strip() for d in depends_on.split(",") if d.strip()]
            if depends_on
            else []
        )
        resolved_agent = assigned_agent.strip() or agent_name

        # Validate assignee against configured agents
        configured = _configured_agent_names()
        if resolved_agent and configured and resolved_agent not in configured:
            available = ", ".join(sorted(configured)) or "none"
            return f"Unknown agent '{resolved_agent}'. Available: {available}"

        def create_item(data: dict[str, Any]) -> dict[str, Any] | str:
            _ensure_thread_state(data, room_id, thread_id)
            items_by_id = {i["id"]: i for i in data["items"]}

            for dep_id in dep_ids:
                if dep_id not in items_by_id:
                    return f"Dependency `{dep_id}` not found."

            existing_ids = {i["id"] for i in data["items"]}
            new_id = _short_id(existing_ids)
            now = _now_iso()
            item = {
                "id": new_id,
                "title": title,
                "status": "open",
                "priority": priority,
                "depends_on": dep_ids,
                "assigned_agent": resolved_agent,
                "event_id": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }

            # Check cycles
            items_by_id[new_id] = item
            for dep_id in dep_ids:
                if _would_create_cycle(items_by_id, new_id, dep_id):
                    return f"Adding dependency `{dep_id}` would create a cycle."

            data["items"].append(item)
            data["updated_at"] = now
            return item

        result = _locked_update_json(path, create_item)
        if isinstance(result, str):
            return result

        emoji = PRIORITY_EMOJI.get(priority, "")
        msg = f"Created: {emoji} `{result['id']}` **{title}** [{priority}]"
        if dep_ids:
            msg += f" (depends on {', '.join(f'`{d}`' for d in dep_ids)})"
        if resolved_agent:
            msg += f" assigned to {resolved_agent}"
        return msg

    def complete_todo(
        self,
        agent: Agent | Team,
        todo_id: str,
    ) -> str:
        """Mark a todo item as completed.

        Args:
            agent: The calling agent (injected automatically).
            todo_id: The short ID of the todo to complete (e.g. "a1b2c3d4").

        Returns:
            Confirmation message, including any items that became unblocked.

        """
        state_root, room_id, thread_id, _ = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        def mark_done(data: dict[str, Any]) -> str:
            _ensure_thread_state(data, room_id, thread_id)
            for item in data["items"]:
                if item["id"] == todo_id:
                    if item["status"] in TERMINAL_STATUSES:
                        return f"Item `{todo_id}` is already {item['status']}."
                    item["status"] = "done"
                    item["completed_at"] = _now_iso()
                    item["updated_at"] = _now_iso()
                    data["updated_at"] = _now_iso()
                    unblocked = _newly_unblocked(data["items"], todo_id)
                    msg = f"\u2705 Completed: **{item['title']}** (`{todo_id}`)"
                    if unblocked:
                        names = ", ".join(
                            f"`{u['id']}` {u['title']}" for u in unblocked
                        )
                        msg += f"\nNow unblocked: {names}"
                    return msg
            return f"Todo `{todo_id}` not found."

        return _locked_update_json(path, mark_done)

    def list_todos(
        self,
        agent: Agent | Team,
        show_all: bool = False,
    ) -> str:
        """List todo items in the current thread's work plan.

        Args:
            agent: The calling agent (injected automatically).
            show_all: If True, include done and cancelled items.

        Returns:
            Formatted list of matching todos.

        """
        state_root, room_id, thread_id, _ = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)
        state = _read_json(path)
        items = state.get("items", [])

        if not items:
            return "No items in this thread's work plan."

        items_by_id = {item["id"]: item for item in items}
        actionable = [i for i in items if is_actionable(i, items_by_id)]
        blocked = [
            i for i in items if i["status"] == "open" and is_blocked(i, items_by_id)
        ]
        done = [i for i in items if i["status"] in TERMINAL_STATUSES]

        actionable.sort(
            key=lambda i: PRIORITY_ORDER.get(i.get("priority", "medium"), 9)
        )

        total = len(items)
        done_count = len(done)
        result_lines = [f"Work plan: {done_count}/{total} complete.\n"]

        if actionable:
            result_lines.append("**Actionable:**")
            for i in actionable:
                emoji = PRIORITY_EMOJI.get(i.get("priority", "medium"), "")
                assigned = f" @{i['assigned_agent']}" if i.get("assigned_agent") else ""
                result_lines.append(
                    f"- {emoji} `{i['id']}` {i['title']} [{i.get('priority', 'medium')}]{assigned}"
                )

        if blocked:
            result_lines.append("\n**Blocked:**")
            for i in blocked:
                waiting = [
                    d
                    for d in i.get("depends_on", [])
                    if items_by_id.get(d, {}).get("status") not in TERMINAL_STATUSES
                ]
                waiting_str = ", ".join(f"`{d}`" for d in waiting)
                result_lines.append(
                    f"- `{i['id']}` {i['title']} waiting on {waiting_str}"
                )

        if show_all and done:
            result_lines.append("\n**Done/Cancelled:**")
            for i in done:
                mark = "\u2705" if i["status"] == "done" else "\u274c"
                result_lines.append(f"- {mark} `{i['id']}` {i['title']}")

        return "\n".join(result_lines)

    def update_todo(
        self,
        agent: Agent | Team,
        todo_id: str,
        title: str = "",
        priority: str = "",
        status: str = "",
        depends_on: str = "",
        assigned_agent: str = "",
    ) -> str:
        """Update fields on an existing todo item.

        Args:
            agent: The calling agent (injected automatically).
            todo_id: The short ID of the todo to update.
            title: New title (leave empty to keep current).
            priority: New priority: low, medium, high, critical (leave empty to keep).
            status: New status: open, done, cancelled (leave empty to keep).
            depends_on: Comma-separated dependency IDs, replaces existing (leave empty to keep).
            assigned_agent: New agent assignment (leave empty to keep).

        Returns:
            Confirmation with updated todo details.

        """
        state_root, room_id, thread_id, _ = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        if priority and priority.lower() not in VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. Must be: low, medium, high, critical."
            )
        if status and status.lower() not in {"open", "done", "cancelled"}:
            return f"Invalid status '{status}'. Must be: open, done, cancelled."

        # Validate assignee against configured agents
        if assigned_agent and assigned_agent.strip():
            configured = _configured_agent_names()
            if configured and assigned_agent.strip() not in configured:
                available = ", ".join(sorted(configured)) or "none"
                return (
                    f"Unknown agent '{assigned_agent.strip()}'. Available: {available}"
                )

        def do_update(data: dict[str, Any]) -> str:
            _ensure_thread_state(data, room_id, thread_id)
            items_by_id = {i["id"]: i for i in data["items"]}
            if todo_id not in items_by_id:
                return f"Todo `{todo_id}` not found."

            item = items_by_id[todo_id]
            changes: list[str] = []

            if title:
                item["title"] = title
                changes.append(f"title='{title}'")
            if priority:
                item["priority"] = priority.lower()
                changes.append(f"priority={priority.lower()}")
            if status:
                new_status = status.lower()
                item["status"] = new_status
                if new_status == "done":
                    item["completed_at"] = _now_iso()
                else:
                    item["completed_at"] = None
                changes.append(f"status={new_status}")
            if depends_on:
                dep_ids = [d.strip() for d in depends_on.split(",") if d.strip()]
                for dep_id in dep_ids:
                    if dep_id not in items_by_id:
                        return f"Dependency `{dep_id}` not found."
                    if dep_id == todo_id:
                        return "Cannot depend on itself."
                    if _would_create_cycle(items_by_id, todo_id, dep_id):
                        return f"Adding dependency `{dep_id}` would create a cycle."
                item["depends_on"] = dep_ids
                changes.append(f"depends_on={dep_ids}")
            if assigned_agent:
                item["assigned_agent"] = assigned_agent.strip()
                changes.append(f"assigned={assigned_agent.strip()}")

            if not changes:
                return "No fields to update."

            item["updated_at"] = _now_iso()
            data["updated_at"] = _now_iso()

            unblocked_msg = ""
            if status and status.lower() in TERMINAL_STATUSES:
                unblocked = _newly_unblocked(data["items"], todo_id)
                if unblocked:
                    names = ", ".join(f"`{u['id']}` {u['title']}" for u in unblocked)
                    unblocked_msg = f"\nNow unblocked: {names}"

            return f"Updated `{todo_id}`: {', '.join(changes)}{unblocked_msg}"

        return _locked_update_json(path, do_update)

    def workloop_apply_template(
        self,
        agent: Agent | Team,
        name: str,
        params: dict[str, str | int | bool],
        dry_run: bool = False,
    ) -> str:
        """Apply a named todo template to the current thread's work plan.

        Args:
            agent: The calling agent (injected automatically).
            name: Template name from `templates/<name>.yaml.j2`.
            params: Template parameters. All required params declared by the
                template must be present.
            dry_run: If True, return a preview of the rendered todos without
                writing `todos.json`.

        Example:
            workloop_apply_template(
                name="mindroom-dev",
                params={"ISSUE_REF": "ISSUE-201", "REPO": "mindroom"},
            )

        """
        template_roots = _visible_template_roots()
        rendered_template = _render_template_definition(
            name,
            params,
            template_roots=template_roots,
        )
        if dry_run:
            return _format_template_preview(
                rendered_template["name"],
                rendered_template["version"],
                rendered_template["resolved_params"],
                rendered_template["todos"],
            )

        state_root, room_id, thread_id, agent_name = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        def apply_template(data: dict[str, Any]) -> list[dict[str, Any]]:
            _ensure_thread_state(data, room_id, thread_id)
            existing_ids = {item["id"] for item in data["items"]}
            created: list[dict[str, Any]] = []
            now = _now_iso()
            for template_todo in rendered_template["todos"]:
                new_id = _short_id(existing_ids)
                existing_ids.add(new_id)
                item = {
                    "id": new_id,
                    "title": template_todo["title"],
                    "status": "open",
                    "priority": template_todo.get("priority", "medium"),
                    "depends_on": [],
                    "assigned_agent": template_todo.get("assigned_agent") or agent_name,
                    "event_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                created.append(item)

            for item, template_todo in zip(
                created, rendered_template["todos"], strict=True
            ):
                item["depends_on"] = [
                    created[dep_index - 1]["id"]
                    for dep_index in template_todo.get("depends_on", [])
                ]

            data["items"].extend(created)
            data["updated_at"] = now
            return created

        created_items = _locked_update_json(path, apply_template)
        return _format_template_apply_result(
            rendered_template["name"],
            rendered_template["version"],
            rendered_template["resolved_params"],
            created_items,
        )

    def workloop_list_templates(
        self,
        agent: Agent | Team,
    ) -> str:
        """List available workloop templates."""
        templates: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for template_root in _visible_template_roots():
            templates_root = template_root.path.resolve()
            if not templates_root.is_dir():
                continue
            for path in sorted(templates_root.glob("*.yaml.j2")):
                resolved_path = path.resolve()
                if not resolved_path.is_relative_to(templates_root):
                    raise ValueError(
                        f"Template '{path.name}' escapes templates dir via symlink"
                    )
                metadata = _load_template_metadata(path)
                if metadata["name"] in seen_names:
                    continue
                seen_names.add(metadata["name"])
                schema = PARAMS_SCHEMAS.get(metadata["name"])
                templates.append(
                    {
                        "source": template_root.source,
                        "name": metadata["name"],
                        "version": metadata["version"],
                        "description": metadata["description"],
                        "json_schema": (
                            json.dumps(schema.model_json_schema(), sort_keys=True)
                            if schema is not None
                            else None
                        ),
                    }
                )
        return _format_templates_table(templates)


# ══════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════


@register_tool_with_metadata(
    name="workloop_todo_manager",
    display_name="Workloop Todo Manager",
    description="Create and manage per-thread work plans with dependencies.",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiTodoist",
    icon_color="text-blue-500",
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
)
def workloop_todo_manager_factory() -> type[WorkloopTodoManager]:
    """Factory function for the WorkloopTodoManager toolkit."""
    return WorkloopTodoManager
