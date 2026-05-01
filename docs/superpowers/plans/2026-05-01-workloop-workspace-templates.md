# Workloop Workspace Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Workloop list and apply templates from the current agent workspace before falling back to bundled plugin templates.

**Architecture:** Add a small template-root resolver inside `tools.py` that derives the current shared or private agent workspace from MindRoom runtime context. Existing render/apply functions will accept ordered template roots, preserve bundled schema behavior, and keep all todo-state writes unchanged.

**Tech Stack:** Python, Pydantic, Jinja2, PyYAML, pytest, MindRoom plugin runtime context.

---

### Task 1: Workspace Template Resolution Tests

**Files:**
- Modify: `tests/test_templates.py`

- [ ] **Step 1: Write failing tests**

Add tests that monkeypatch runtime context and MindRoom workspace helpers:

- `test_workspace_template_overrides_builtin_template`
- `test_list_templates_prefers_workspace_template_source`
- `test_apply_workspace_template_writes_todos`
- `test_workspace_template_can_expand_builtin_sub_template`
- `test_include_builtin_templates_false_hides_builtin_templates`
- `test_workspace_templates_use_private_resolved_workspace`
- `test_list_templates_rejects_workspace_symlink_escape`

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run --project ../mindroom pytest tests/test_templates.py -q
```

Expected: new tests fail because workspace roots and `include_builtin_templates` do not exist.

### Task 2: Template Root Resolver

**Files:**
- Modify: `tools.py`

- [ ] **Step 1: Implement minimal resolver**

Add focused helpers:

```python
WORKSPACE_TEMPLATE_RELATIVE_DIR = Path("workloop/templates")

def _include_builtin_templates(settings: Mapping[str, Any] | None) -> bool:
    return (settings or {}).get("include_builtin_templates") is not False

def _current_agent_workspace_root() -> Path | None:
    ctx = get_tool_runtime_context()
    if ctx is None:
        return None
    return agent_workspace_root_path(ctx.runtime_paths.storage_root, ctx.agent_name)

def _visible_template_roots(settings: Mapping[str, Any] | None = None) -> tuple[TemplateRoot, ...]:
    roots = []
    workspace_root = _current_agent_workspace_root()
    if workspace_root is not None:
        roots.append(TemplateRoot(workspace_root / "workloop/templates", "workspace"))
    if _include_builtin_templates(settings):
        roots.append(TemplateRoot(_templates_dir(), "builtin"))
    return tuple(roots)
```

Shared agents use `agent_workspace_root_path(ctx.runtime_paths.storage_root, ctx.agent_name)`.
Private agents use `resolve_agent_runtime(..., build_execution_identity_from_runtime_context(ctx), create=True).workspace.root`.

- [ ] **Step 2: Run tests to verify GREEN for resolver cases**

Run:

```bash
uv run --project ../mindroom pytest tests/test_templates.py -q
```

Expected: resolver-related tests pass or fail only on list/apply integration.

### Task 3: Ordered Lookup Integration

**Files:**
- Modify: `tools.py`

- [ ] **Step 1: Update rendering/listing**

Change template lookup from one `template_dir` to ordered roots:

```python
def _resolve_template_path(name: str, template_roots: Sequence[TemplateRoot]) -> Path:
    for template_root in template_roots:
        path = _template_path(name, template_root.path)
        if path.is_file():
            return path
    raise ValueError(f"Unknown template: {name!r}")
```

Use the same ordered roots for sub-template expansion. Update `workloop_apply_template()` and `workloop_list_templates()` to pass current visible roots.

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run --project ../mindroom pytest tests/test_templates.py -q
```

Expected: all template tests pass.

### Task 4: Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document workspace templates**

Add a short note under Templates:

```markdown
Agents can also define templates in `<agent-workspace>/workloop/templates/*.yaml.j2`.
Workspace templates are discovered before bundled templates and override by name.
Set `include_builtin_templates: false` in plugin settings to disable bundled fallback.
```

- [ ] **Step 2: Run verification**

Run:

```bash
uv run --project ../mindroom pytest tests/test_templates.py -q
uv run --project ../mindroom pytest tests/test_templates.py tests/test_tool_schema.py tests/test_hooks.py -q
```

Expected: tests pass.

### Task 5: Commit And PR

**Files:**
- Commit all changed files.

- [ ] **Step 1: Inspect diff**

Run:

```bash
git diff --stat
git diff
```

- [ ] **Step 2: Commit**

Run:

```bash
git add tools.py tests/test_templates.py README.md docs/superpowers/plans/2026-05-01-workloop-workspace-templates.md
git commit -m "feat: load workloop templates from agent workspace"
```

- [ ] **Step 3: Push and open PR**

Run:

```bash
git push -u origin workloop-workspace-templates
gh pr create --title "Load workloop templates from agent workspace" --body-file /tmp/workloop-workspace-templates-pr.md
```
