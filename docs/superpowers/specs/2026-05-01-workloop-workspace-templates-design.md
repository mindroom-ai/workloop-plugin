# Workloop Workspace Templates Design

Date: 2026-05-01

## Goal

Workloop should let users define todo templates in an agent's canonical workspace. Agents can then list and apply those templates with the existing `workloop_list_templates()` and `workloop_apply_template()` tools, without hard-coding project-specific protocols in the plugin checkout.

The first iteration should stay small:

- Workspace templates are discovered before bundled plugin templates.
- Bundled plugin templates remain the fallback by default.
- A single plugin setting can disable bundled plugin templates entirely.
- Existing todo state, YAML/Jinja rendering, dependency expansion, and hard-coded built-in schemas remain unchanged.

## Template Location

The workspace template root is:

```text
<agent-workspace>/workloop/templates/
```

Template files keep the current format:

```text
<agent-workspace>/workloop/templates/<name>.yaml.j2
```

For normal shared agents, the workspace root is the canonical shared agent workspace:

```text
<storage_root>/agents/<agent>/workspace/
```

This applies even when the agent is not using file memory. The feature should not require `memory_backend: file`.

For private agents, the workspace root is the already-resolved private agent workspace for the current requester/execution identity. This keeps templates scoped the same way as private context files and private knowledge.

## Lookup Rules

Template lookup uses ordered roots:

1. Current agent workspace template root, if it exists.
2. Bundled plugin `templates/`, unless disabled by config.

Workspace templates win name collisions. If both roots contain `mindroom-dev.yaml.j2`, `workloop_apply_template("mindroom-dev", ...)` uses the workspace file.

Sub-template expansion uses the same ordered roots, not only the root of the parent template. This means a workspace template can reference:

- another workspace template by name
- a bundled template by name when bundled fallback is enabled

If bundled fallback is disabled, sub-template expansion only sees workspace templates.

## Plugin Setting

Add one setting:

```yaml
plugins:
  - path: plugins/workloop
    settings:
      include_builtin_templates: true
```

Default: `true`.

When `include_builtin_templates: false`, Workloop ignores plugin-bundled templates for both listing and applying. Workspace templates still work.

Invalid setting values should be handled conservatively: treat omitted as `true`, and treat only explicit boolean `false` as disabled.

## Tool Behavior

`workloop_list_templates()` should merge visible templates from the ordered roots and return one row per template name. Rows should include enough source information for agents to understand overrides, for example `workspace` or `builtin`.

`workloop_apply_template(name, params, dry_run=False)` should resolve `name` from the same ordered roots. The rest of apply behavior stays as-is:

- render YAML/Jinja
- validate todo document shape
- expand sub-templates
- validate dependency indexes and cycles
- on dry run, return preview only
- on apply, append new todos atomically to the current thread state

## Parameter Schemas

Keep parameter schema handling simple for the first iteration.

Existing hard-coded schemas in `template_schemas.py` continue to apply by template name. Workspace templates without a hard-coded schema accept arbitrary params, as plugin ad-hoc templates already do today.

This intentionally does not add inline JSON Schema support yet. That can come later if user-authored templates need strict validation.

## Safety

The implementation should keep the current path safety constraints:

- Template names are stems only, not paths.
- Absolute paths, `..`, `/`, and `\` are rejected.
- Symlink escapes from any template root are rejected.
- Template files must stay under their resolved template root.

Workspace template roots are read-only from the plugin's perspective. The plugin lists and applies templates, but does not create or edit template files.

## Tests

Add focused tests in `tests/test_templates.py`:

- workspace template is listed before bundled templates
- workspace template overrides a bundled template with the same name
- applying a workspace template writes todos correctly
- applying a workspace template can expand a bundled sub-template when fallback is enabled
- `include_builtin_templates: false` hides and prevents applying bundled-only templates
- private workspace resolution uses the current requester/execution identity
- normal shared agents can use `agents/<agent>/workspace/workloop/templates` even when not file-memory agents
- symlink escape protection applies to workspace template roots

## Out Of Scope

- Editing templates through Workloop tools
- Inline template schema declarations
- Multiple configured template directories
- Global shared template libraries
- Changes to todo state format or auto-poke behavior
