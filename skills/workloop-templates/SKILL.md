---
name: workloop-templates
description: Use when creating, editing, reviewing, or troubleshooting Workloop todo templates for MindRoom agents.
---

# Workloop Templates

Workloop templates are YAML/Jinja files that `workloop_apply_template` expands into todos. Prefer user templates in the agent workspace so project workflow stays with the agent instead of the plugin checkout.

## Location

Put templates here:

```text
<agent-workspace>/workloop/templates/<template-name>.yaml.j2
```

For normal shared agents, `<agent-workspace>` is the canonical shared workspace for that agent. For private agents, it is the resolved private workspace for the current requester/private execution scope.

Workspace templates are searched before bundled plugin templates. A workspace template with the same name overrides the bundled one. Set plugin setting `include_builtin_templates: false` when bundled templates should not be visible or usable as fallback.

## Schema Contract

Use a template file stem as the lookup name, for example `release-checklist.yaml.j2` is applied as `release-checklist`.

```yaml
name: release-checklist
version: "1.0"
description: Prepare and verify a release.
todos:
  - title: "Create release branch {{ BRANCH }}"
    priority: high
    assigned_agent: codex
  - title: "Run release tests"
    depends_on: [1]
  - sub_template: parallel-review-loop
    params:
      N_REVIEWERS: 3
    depends_on: [2]
```

The top level must be a YAML mapping with exactly these fields. Extra fields are rejected.

| field | type | rule |
| --- | --- | --- |
| `name` | string | Required. Must exactly match the filename stem before and after Jinja rendering. Do not template this field. |
| `version` | string | Required. |
| `description` | string | Required. |
| `todos` | list | Required. Must contain at least one todo entry. |

Each todo entry is exactly one of these shapes. Extra fields are rejected.

Normal todo:

```yaml
- title: "Do the work"
  priority: medium
  depends_on: [1]
  assigned_agent: codex
```

| field | type | rule |
| --- | --- | --- |
| `title` | string | Required for a normal todo. |
| `priority` | `low`, `medium`, `high`, `critical` | Optional. Defaults to `medium`. |
| `depends_on` | list of integers | Optional. Defaults to `[]`. |
| `assigned_agent` | string | Optional. |

Normal todos must not include `sub_template` or `params`.

Sub-template todo:

```yaml
- sub_template: parallel-review-loop
  params:
    N_REVIEWERS: 3
  depends_on: [1]
```

| field | type | rule |
| --- | --- | --- |
| `sub_template` | string | Required for a sub-template todo. Resolved from the same workspace-first roots. |
| `params` | mapping | Optional. Defaults to `{}`. Passed to the child template. |
| `depends_on` | list of integers | Optional. Defaults to `[]`. |

Sub-template todos must not include `title`, `priority`, or `assigned_agent`.

## Dependencies

`depends_on` uses 1-based indexes into the current rendered template's own `todos` list, before sub-template expansion. Every index must be in range `1..len(todos)`.

Dependency cycles are rejected after sub-templates are expanded. When a todo depends on a sub-template entry, it depends on that sub-template's last expanded todo. When a sub-template entry has `depends_on`, the dependency is applied to the first expanded child.

## Parameters

Template params are available as Jinja variables:

```yaml
todos:
  - title: "Fix {{ ISSUE_REF }} on {{ REPO }}"
```

Top-level `workloop_apply_template` params are scalar values: strings, integers, and booleans. User-defined templates have no declared param schema in the first iteration; passed params are made available to Jinja as-is, and missing variables fail loudly.

Built-in schemas still apply by template name:

- `mindroom-dev`: requires `ISSUE_REF: str` and `REPO: mindroom|cinny|nixos|tuwunel`; optional `BRANCH: str`, `N_REVIEWERS: int >= 1`, `IMPLEMENTER_AGENT: str`, `IS_PR: bool`, `BASE: origin/main|main`; extra params rejected. Empty `BRANCH` defaults to `ISSUE_REF.lower()`. Omitted `BASE` defaults from `IS_PR`.
- `parallel-review-loop`: optional `N_REVIEWERS: int >= 1`; extra params rejected.

## Safety Checks

Template names are stems only, not paths. Do not use `/`, `\`, `..`, or absolute paths. Workloop rejects template symlinks that resolve outside the template root. Sub-template expansion stops after depth 3.

YAML is parsed with safe loading. Jinja rendering is sandboxed and uses strict undefined variables. Templates create todos only; they do not execute shell commands or write template files.

## Workflow

1. Create or edit `<agent-workspace>/workloop/templates/<name>.yaml.j2`.
2. Run `workloop_list_templates` and confirm the template appears with source `workspace`.
3. Run `workloop_apply_template(name, params, dry_run=true)` first.
4. Apply without dry run only after the preview shows the intended todos and dependencies.
