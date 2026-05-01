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

## File Format

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

Required top-level fields: `name`, `version`, `description`, `todos`.

Each todo is exactly one of:

- `title`: a normal todo. Optional fields: `priority`, `depends_on`, `assigned_agent`.
- `sub_template`: another template to inline. Optional fields: `params`, `depends_on`.

`priority` must be one of `low`, `medium`, `high`, or `critical`. `depends_on` uses 1-based indexes in the expanded template list.

## Parameters

Template params are available as Jinja variables:

```yaml
todos:
  - title: "Fix {{ ISSUE_REF }} on {{ REPO }}"
```

User-defined template params are intentionally lightweight: arbitrary scalar params can be passed unless the rendered template `name` matches a built-in hard-coded schema such as `mindroom-dev` or `parallel-review-loop`.

## Safety Checks

Template names are stems only, not paths. Do not use `/`, `\`, `..`, or absolute paths. Workloop rejects template symlinks that resolve outside the template root.

Jinja rendering is sandboxed and uses strict undefined variables, so missing params fail loudly. Templates create todos only; they do not execute shell commands or write template files.

## Workflow

1. Create or edit `<agent-workspace>/workloop/templates/<name>.yaml.j2`.
2. Run `workloop_list_templates` and confirm the template appears with source `workspace`.
3. Run `workloop_apply_template(name, params, dry_run=true)` first.
4. Apply without dry run only after the preview shows the intended todos and dependencies.
