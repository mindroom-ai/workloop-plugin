# ISSUE-159 — Final Synthesized Plan

**Source plans:**
- `../workloop-worktrees/issue-159-plan-codex/PLAN.md` (Codex, commit 48a5845)
- `../workloop-worktrees/issue-159-plan-claude/PLAN.md` (Claude, commit beba9a4)

Both planners converged on ~95% of the design. Synthesis below resolves the remaining 8 open questions and is the binding spec for implementation.

---

## Scope

Add to the WorkLoop plugin:
1. `workloop_apply_template(name, params, dry_run=False) -> str` (markdown summary)
2. `workloop_list_templates() -> str` (markdown table)
3. `templates/mindroom-dev.yaml.j2`
4. `templates/parallel-review-loop.yaml.j2`

Plus:
5. SOUL.md Rule #1d
6. `safe-squash-merge.sh -F <path>` flag

No refactors of existing code. No new toolkit class.

---

## 1. Template format

**Path:** `~/.mindroom-chat/plugins/workloop/templates/<name>.yaml.j2`
**Engine:** Jinja2 over YAML. Read fresh on every call (no caching).

Templates are now **pure data**. The YAML no longer declares parameter types,
defaults, or choices. Those live in Python.

**Top-level shape:**

| Key | Type | Required |
|---|---|---|
| `name` | str | yes (must match filename stem) |
| `version` | str | yes (free-form) |
| `description` | str | yes (one line) |
| `todos` | list | yes |

**`todos[i]` — exactly one shape:**

```yaml
# Shape A: regular todo
- title: "..."             # required
  priority: high           # optional, default "medium" (low|medium|high|critical)
  depends_on: [1, 3]       # optional, 1-based indexes into FINAL flattened list
  assigned_agent: "..."    # optional
```

```yaml
# Shape B: sub-template reference
- sub_template: parallel-review-loop  # required
  params: { N_REVIEWERS: "{{ N_REVIEWERS }}" }  # optional
  depends_on: [6]                      # optional, applied to FIRST expanded child only
```

**Sub-template inlining:**
- Splice expanded list in place of the `sub_template:` entry.
- Parent's `depends_on` for the sub-template entry → added to FIRST child's `depends_on`.
- Parent todos that listed the sub-template's index as a dep → after expansion, refer to LAST child of expansion (the "exit" point — matches loop semantics).
- Recursion depth cap = **3**. Beyond → `ValueError`.
- Empty templates are rejected structurally by the Python schema (`todos` min length = 1).

---

## 2. Parameter substitution + validation

**Python module:** `~/.mindroom-chat/plugins/workloop/template_schemas.py`

This module defines exactly four Pydantic models:
- `MindroomDevParams`
- `ParallelReviewLoopParams`
- `Todo`
- `TemplateDocument`

And one registry dict:

```python
PARAMS_SCHEMAS = {
    "mindroom-dev": MindroomDevParams,
    "parallel-review-loop": ParallelReviewLoopParams,
}
```

Rules:
- The registry key matches the template filename stem.
- Registered templates validate params by instantiating the mapped Pydantic model.
- Templates without a registry entry accept any params dict unchanged.
- Derived defaults such as `BRANCH = ISSUE_REF.lower()` and `BASE = "origin/main" if IS_PR else "main"` live in Python, not in YAML.

Order of operations in `workloop_apply_template(name, params)`:

1. Resolve + sanitize the template path.
2. Load raw YAML and validate the document shape with `TemplateDocument`.
3. Look up `PARAMS_SCHEMAS[name]`; if present, validate params with Pydantic and `model_dump()` them. If absent, accept the supplied params dict as-is.
4. Render the template with Jinja2 + resolved params.
5. Parse rendered YAML and validate again with `TemplateDocument`.
6. Validate `depends_on` indexes against the rendered todo count.
7. Expand sub-templates (recursive, depth-capped).
8. Renumber dependencies (build pre→post index map; rewrite parent deps using LAST-child rule).

All failures `raise ValueError(...)` — agent sees the error string.

---

## 3. Tool implementation

Two new methods on existing `WorkloopTodoManager` toolkit in `tools.py`. No new toolkit class.

### `workloop_apply_template(self, agent, name: str, params: dict, dry_run: bool = False) -> str`

```
1. Resolve template path: Path(__file__).parent / "templates" / f"{name}.yaml.j2"
   Reject unknown templates with clear error.
2. Validate the raw document with TemplateDocument.
3. Validate params through PARAMS_SCHEMAS[name] if the template is registered;
   otherwise accept the supplied params dict unchanged.
4. Render + validate the final document. Result: list of expanded todo dicts
   with 1-based numeric depends_on.
5. If dry_run → return formatted markdown preview (titles, priorities, deps).
   No file write. Include the resolved params + template version.
6. Under existing _locked_update_json(path, mutate):
   a. Generate one short_id per template todo (use existing _short_id helper,
      seeded with current data["items"] ids to avoid collisions).
   b. Map index → short_id.
   c. Translate depends_on (int index → short_id string).
   d. Build final todo dicts matching existing add_todo shape (status="open",
      created_at, updated_at, priority, assigned_agent default to caller agent
      id, etc.).
   e. Append all to data["items"] in single mutate call.
7. Return formatted markdown summary: template name, version, resolved params,
   list of (id, title) created, count.
```

**Atomicity:** all validation/render/expansion/dep-translation runs OUTSIDE the lock (pure functions). Inside the lock: single `mutate` callback that appends every todo. If `mutate` raises, the file isn't written. fcntl already gives all-or-nothing.

### `workloop_list_templates(self, agent) -> str`

Glob `templates/*.yaml.j2` sorted. For each:

1. Parse YAML without rendering.
2. Read metadata only: `name`, `version`, `description`.
3. Look up `PARAMS_SCHEMAS.get(name)`.
4. Emit `schema.model_json_schema()` when registered, otherwise `-`.

Listing does **not** render template bodies and does **not** run apply-time todo validation. That removes the old placeholder-synthesis branch entirely. Return a markdown table: name | version | description | json schema.

**Return type for both tools: `str`** (markdown). Matches Agno toolkit convention used elsewhere in this plugin. The spec said `dict` but the plugin's existing tools all return strings, and consistency wins.

---

## 4. Dependencies

- `pyyaml`: already a hard dep of mindroom. Available.
- `jinja2`: transitively present in mindroom env (visible in uv.lock). Verify with `python -c "import jinja2"` in mindroom runtime as a Phase 1 prereq. If missing, add `"jinja2>=3"` to `/srv/mindroom/pyproject.toml` (out-of-tree change, mention in commit).

The workloop plugin has no `pyproject.toml` of its own — inherits mindroom's env. Don't create one.

---

## 5. Tool registration

Add `self.workloop_apply_template` and `self.workloop_list_templates` to the `tools=[...]` list in `WorkloopTodoManager.__init__`. **No `mindroom.plugin.json` change.**

---

## 6. Test plan

New file: `tests/test_templates.py`. Use existing `_load_tools_module()` pattern; no fixture changes.

**Unit tests:**
- `test_render_basic_template` — single template, no sub, no params, returns N todos with correct deps
- `test_pydantic_validates_required_params` — `MindroomDevParams(ISSUE_REF="x")` fails because `REPO` is missing
- `test_pydantic_validates_choices` — invalid `REPO` is rejected by Pydantic
- `test_pydantic_validates_n_reviewers_range` — `N_REVIEWERS=0` is rejected by `Field(ge=1)`
- `test_branch_defaults_from_issue_ref` — `BRANCH` derives from `ISSUE_REF.lower()`
- `test_base_defaults_from_is_pr` — `BASE` resolves to `origin/main` or `main` from `IS_PR`
- `test_sub_template_inlining` — flat list correct length and order
- `test_sub_template_dep_first_child` — parent's `depends_on` lands on FIRST expanded child
- `test_sub_template_dep_last_child` — parent's *consumers* point at LAST expanded child after renumbering
- `test_invalid_priority_in_template_fails_loud_on_apply` — invalid todo priority is rejected during apply
- `test_out_of_range_depends_on_in_template_fails_loud_on_apply` — invalid todo dependency index is rejected during apply
- `test_list_templates_returns_metadata_no_rendering` — listing succeeds even if apply-time rendering would fail
- `test_no_schema_template_accepts_any_params` — unregistered templates accept arbitrary params
- `test_dry_run_does_not_write` — no mutation
- `test_dry_run_returns_preview` — preview includes titles + deps
- `test_apply_writes_atomically` — mindroom-dev → exactly **20** todos
- `test_dependency_chain_intact` — pick a leaf, walk deps, every step from root reachable
- `test_recursion_depth_cap` — 4-deep chain → ValueError
- `test_unknown_template_raises` — clear error
- `test_malformed_template_fails_loud` — list_templates raises with filename

**Integration smoke** (manual, in Phase 4 live-test):
- From a real thread, call `workloop_apply_template("mindroom-dev", {"ISSUE_REF": "ISSUE-TEST", "REPO": "mindroom"})`
- Verify 20 todos via `list_todos` with correct gates and deps

**Script tests** (extend `test-safe-squash-merge.sh`):
- `-F` happy path: multi-paragraph file, body matches exactly
- `-F` mutual exclusion with positional subject → exit 1
- `-F` missing file → exit 1

---

## 7. SOUL.md Rule #1d

**Target:** `/home/basnijholt/.mindroom-chat/mindroom_data/agents/mindroom_dev/workspace/SOUL.md`
**Position:** immediately after Rule #1c.

```markdown
## #1d Rule: Protocol Templates Are Mandatory (added 2026-04-19)

When starting work on a known protocol (`mindroom-dev`, and any future
protocol with a workloop template), the **first tool call MUST be**:

    workloop_apply_template(name="<protocol>", params={...})

This pre-populates the per-thread todo list with every gate as an enforceable
item. Skipping it means the protocol degrades to text-only and gates get
silently dropped under context pressure — exactly the failure mode this rule
exists to prevent.

**Procedure:**
1. Run `workloop_list_templates()` if unsure which templates exist.
2. Call `workloop_apply_template(name, params)` BEFORE any other action
   (no implementer spawn, no report file write, no commit — those are todos
   the template will create).
3. Work the todos in order. Mark each `complete_todo(id)` only when its gate
   has actually been met (file exists / commit landed / evidence captured).

If the protocol you're running has no template, write the template to
`~/.mindroom-chat/plugins/workloop/templates/<name>.yaml.j2` BEFORE proceeding.
A protocol without a template is a protocol that will drift.

**Exception:** trivial one-shot work (typo fix, docs tweak, single-line
config change) does not need a template — those don't follow `mindroom-dev`
in the first place.
```

---

## 8. `safe-squash-merge.sh` change

**Path:** `/home/basnijholt/.mindroom-chat/mindroom_data/agents/mindroom_dev/workspace/skills/mindroom-dev/scripts/safe-squash-merge.sh`

Add `-F <path>` flag, **flag-first** in the CLI (`-F path branch`), reject combination with positional subject/body, reject missing file. Pass through to `git commit -F`.

**Do NOT add a `BASE` argument.** The script always merges into local main; that's intentional. The `BASE` parameter in the template is for the **diff sanity check** step (`git diff origin/main...branch`), not for the merge target.

```bash
# arg parse (top of script, after set -euo pipefail):
COMMIT_FILE=""
while [[ $# -gt 0 && "$1" == -* ]]; do
    case "$1" in
        -F)
            COMMIT_FILE="$2"; shift 2 ;;
        *)
            echo "usage: $0 [-F <commit-msg-file>] <branch> [<subject> [<body>]]" >&2
            exit 1 ;;
    esac
done

if [[ -n "$COMMIT_FILE" ]]; then
    if [[ $# -ne 1 ]]; then
        echo "error: -F is mutually exclusive with positional subject/body" >&2
        exit 1
    fi
    if [[ ! -r "$COMMIT_FILE" ]]; then
        echo "error: commit message file not readable: $COMMIT_FILE" >&2
        exit 1
    fi
    BRANCH="$1"
    SUBJECT=""; BODY=""
else
    if [[ $# -lt 2 || $# -gt 3 ]]; then
        echo "usage: $0 <branch> <commit-subject> [<commit-body>]" >&2
        exit 1
    fi
    BRANCH="$1"; SUBJECT="$2"; BODY="${3:-}"
fi

# ... existing pre-flight + merge logic unchanged ...

# commit:
if [[ -n "$COMMIT_FILE" ]]; then
    git commit -F "$COMMIT_FILE"
elif [[ -n "$BODY" ]]; then
    git commit -m "$SUBJECT" -m "$BODY"
else
    git commit -m "$SUBJECT"
fi
```

---

## 9. `mindroom-dev.yaml.j2`

```yaml
# mindroom-dev — single-issue development protocol
# Spec: ISSUE-159. Every gate is signal-driven, not time-based.

name: mindroom-dev
version: "1.0"
description: Single-issue dev protocol — file → plan → implement → review-loop → live-test → squash-merge.

todos:
  # 1. Living report — anchors the issue on disk.
  - title: "Create living report at skills/mindroom-dev/references/reports/{{ ISSUE_REF }}.md (gate: file exists)"
    priority: high

  # 2-4. Dual-agent planning.
  - title: "Spawn 2 planners (codex + claude) on {{ BRANCH }}-plan-* worktrees (gate: both running)"
    depends_on: [1]
    priority: high
  - title: "Cross-feed plans → planners debate, each produces a critique (gate: both critiques on disk)"
    depends_on: [2]
  - title: "Synthesize final plan; commit as FIRST commit on {{ BRANCH }} (gate: commit exists on branch)"
    depends_on: [3]
    priority: high

  # 5-6. Implementer — KEEP ALIVE across the whole loop (do not respawn).
  - title: "Spawn implementer ({{ IMPLEMENTER_AGENT }}) on {{ BRANCH }} via tmux session {{ BRANCH }} — KEEP ALIVE for fix cycles (gate: tmux session running)"
    depends_on: [4]
    priority: high
  - title: "Implementer reports complete: commit pushed AND tests pass (gate: signal from implementer)"
    depends_on: [5]

  # 7. Parallel review loop — sub-template, terminates on unanimous APPROVE on same SHA.
  - sub_template: parallel-review-loop
    params:
      N_REVIEWERS: "{{ N_REVIEWERS }}"
    depends_on: [6]

  # 8. Diff sanity check — independent scope-creep guard.
  - title: "Diff sanity check: git diff {{ BASE }}...{{ BRANCH }} — abort if scope exploded beyond plan (gate: manual ack)"
    depends_on: [7]
    priority: high

  # 9-10. Live-test gate — bespoke evidence, NOT 'tests passed in CI'.
  - title: "Capture live-test evidence at /tmp/{{ ISSUE_REF }}-evidence/ (screenshots, curl output, logs) (gate: files exist on disk)"
    depends_on: [7]
    priority: high
  - title: "DevAgent verifies the evidence demonstrates the ACTUAL fix, not generic 'service started' noise (gate: manual ack)"
    depends_on: [9]
    priority: high

  # 11a-11b. Commit message + safe squash merge.
  - title: "Author commit message at /tmp/{{ ISSUE_REF }}-commit-msg.txt — body MUST contain problem/approach/why/how, NO test counts, NO reviewer names, NO file enumerations (gate: file exists, body ≥3 paragraphs)"
    depends_on: [8, 10]
    priority: high
  - title: "Run skills/mindroom-dev/scripts/safe-squash-merge.sh -F /tmp/{{ ISSUE_REF }}-commit-msg.txt {{ BRANCH }} (gate: script exit 0)"
    depends_on: [11]
    priority: critical

  # 12. Cinny-specific deploy step (no-op for other repos but listed unconditionally so it can't be skipped).
  - title: "{% if REPO == 'cinny' %}Cinny deploy: npm run build && sudo systemctl restart mindroom-cinny.service (gate: service active){% else %}No deploy step required for {{ REPO }} (mark complete immediately){% endif %}"
    depends_on: [12]

  # 13-14. Status + thread recap.
  - title: "Update {{ ISSUE_REF }}.md status to 🔍 APPROVAL PENDING (NEVER ✅ FIXED — only Bas verifies) (gate: report file edited)"
    depends_on: [13]
    priority: high
  - title: "Post final thread summary with recap footer (gate: message sent)"
    depends_on: [14]
```

**Expected expansion: 20 todos.** Sub-template inserts 6 children at position 7, shifting later indexes by +5.

---

## 10. `parallel-review-loop.yaml.j2`

```yaml
# parallel-review-loop — N-reviewer code-debate loop.
# Terminates ONLY when all N reviewers APPROVE the same SHA. No manual exit.
# Default mix: 7×codex-xhigh + 1×claude-opus-high.

name: parallel-review-loop
version: "1.0"
description: N-reviewer parallel review loop, terminates on unanimous APPROVE on same SHA.

todos:
  - title: "Spawn N={{ N_REVIEWERS }} reviewers (default 7×codex-xhigh + 1×claude-opus-high) against current implementer SHA (gate: all N spawned)"
    priority: high
  - title: "Collect N={{ N_REVIEWERS }} verdicts (APPROVE / REQUEST_CHANGES) (gate: N verdicts received)"
    depends_on: [1]
  - title: "Triage feedback per SOUL #1b — drop theoretical edges, defensive checks, scope creep, options nobody asked for (gate: triaged set written)"
    depends_on: [2]
    priority: high
  - title: "If all {{ N_REVIEWERS }} APPROVE on same SHA → mark this todo done to exit loop. Else: forward triaged feedback to existing implementer (gate: branch decision made)"
    depends_on: [3]
    priority: high
  - title: "If step 4 forwarded changes: implementer (kept alive) applies fixes, produces new SHA. Else: mark complete immediately as no-op (gate: new commit on branch OR step 4 was unanimous approve)"
    depends_on: [4]
  - title: "Re-spawn review loop on new SHA: prior approvers get fresh /new context window, change-requesters keep context. Loop back by calling workloop_apply_template('parallel-review-loop', ...) again (gate: new round started OR previous round was unanimous and this todo is no-op)"
    depends_on: [5]
```

**Loop semantics:** WorkLoop is linear. The "loop" is encoded as a textual cycle — when reviewers REQUEST_CHANGES, the agent re-applies this same sub-template, appending fresh round todos. The mechanical guarantee is "you cannot complete step 7 of mindroom-dev (the sub-template's exit) without ticking sub-#4 (`unanimous APPROVE on same SHA`)". Sub-#6 ("re-spawn") is the agent's signal to call `workloop_apply_template("parallel-review-loop", ...)` again on the new SHA. Text-driven re-application is acceptable for v1; mechanical loop runtime support is out of scope.

---

## 11. Risks / open questions resolved

| # | Question | Resolution |
|---|---|---|
| 1 | `safe-squash-merge.sh` `BASE` arg | **No.** Script always merges to local main; `BASE` only for diff sanity check |
| 2 | Loop semantics | **Text-driven re-application.** Mechanical loop runtime out of scope |
| 3 | `jinja2` availability | **Verify in Phase 1.** Add to mindroom pyproject only if missing |
| 4 | Plugin `pyproject.toml` | **Don't create.** Inherits mindroom env |
| 5 | Malformed template handling | **Fail loud** with filename in error |
| 6 | Repeated `apply_template` calls | **No de-dup.** User's problem if called twice |
| 7 | Return type (dict vs string) | **String** (markdown), matches Agno convention |
| 8 | CLI arg order | **`-F path branch`** (flag-first) |

---

## 12. Architectural pivot away from placeholder synthesis

The `_list_validation_param_value` approach was removed entirely.

Why:
- It forced `workloop_list_templates()` to invent semantic values for missing params.
- Those invented values changed Jinja control flow and created list/apply mismatches.
- Review rounds R3-R5 all traced back to that architectural error.

New rule:
- YAML templates are pure data.
- Param schemas live in Python (`template_schemas.py`).
- Param validation happens at apply time.
- Template listing is metadata-only and never renders todo bodies.

Effect:
- The optional-no-default synthesis rabbit hole is gone.
- The R3-R5 reviewer findings become structurally impossible.
- Roughly 180 lines of hand-rolled validation and placeholder logic disappear from `tools.py`.

---

## Refactor Sanity Check

**No refactors of existing workloop code proposed.** The plan strictly adds:
- 2 new methods to `WorkloopTodoManager`
- 1 new `templates/` directory with 2 files
- 1 new test file
- 1 SOUL.md rule
- 1 arg-parse block + 1 commit block in `safe-squash-merge.sh`
- Possibly 1 line in `/srv/mindroom/pyproject.toml` (only if `jinja2` missing)

The two new methods could in principle live in a separate `WorkloopTemplateManager` toolkit, but that adds a new toolkit to register and forces agents to remember two toolkit names. **Not worth the split for two methods.** Revisit if methods grow past ~5.
