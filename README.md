# Workloop

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Autonomous, persistent work plans for [MindRoom](https://github.com/mindroom-ai/mindroom) agents.

Without workloop, an agent responds once and stops. With workloop, an agent turns a task into a dependency-aware plan, works through it step by step, and gets nudged to resume when it goes idle. This is more than a todo list. It is a closed-loop execution system built around persistent per-thread state, prompt enrichment, and automatic follow-up.

## Features

- Per-thread work plans with priorities, dependencies, and optional agent assignment
- Tool interface for creating, listing, updating, and completing plan items
- `!todo` command interface for manual control from chat
- Reaction-driven completion and cancellation via `✅` and `❌`
- Prompt enrichment that shows actionable, blocked, and completed work each turn
- Background auto-poke loop that wakes idle agents when actionable work remains
- Schedule-aware poking that skips threads with pending scheduled tasks
- Persistent JSON state under the plugin state root so plans survive restarts

## How It Works

1. An agent creates a work plan with `plan(tasks)` or adds individual items with `add_todo(...)`.
2. Workloop stores the plan as per-thread JSON, including status, priority, dependencies, assignee, and timestamps.
3. On each turn, the `workloop-context` hook injects the current plan into the prompt.
4. After a response, `workloop-track-idle` records that the agent is idle again for that thread.
5. The auto-poke loop scans for actionable work and nudges the assigned agent to continue when the thread is idle and no pending schedule already covers it.

## Agent Tools

Toolkit name: `workloop_todo_manager`

| Tool | Purpose |
|------|---------|
| `plan(tasks)` | Create a work plan from a multi-line task list. Each line becomes one item; `[priority]` prefixes are supported |
| `add_todo(title, depends_on="", priority="medium", assigned_agent="")` | Add a single item with optional dependencies, priority, and assignee |
| `complete_todo(todo_id)` | Mark an item done and report any newly unblocked work |
| `list_todos(show_all=False)` | Show the current plan, optionally including done and cancelled items |
| `update_todo(todo_id, ...)` | Change title, priority, status, dependencies, or assignee |

## Command Interface

Workloop also exposes a chat command interface:

- `!todo help`
- `!todo add <title>`
- `!todo add [high] <title>`
- `!todo list`
- `!todo all`
- `!todo plan`
- `!todo done <id>`
- `!todo cancel <id>`
- `!todo rm <id>`
- `!todo dep <id> <depends-on-id>`
- `!todo assign <id> <agent>`
- `!workloop-tick` for a one-shot manual poke scan

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `auto_poke` | `schedule:fired` | Suppress deprecated scheduled `!workloop-tick` heartbeats |
| `workloop-auto-poke-start` | `agent:started` | Start the background auto-poke loop |
| `workloop-auto-poke-stop` | `agent:stopped` | Stop the background auto-poke loop |
| `workloop-context` | `message:enrich` | Inject the current plan into every prompt |
| `workloop-track-idle` | `message:after_response` | Record when an agent becomes idle again for this thread |
| `workloop-command` | `message:received` | Handle `!todo` and manual `!workloop-tick` commands |
| `workloop-react` | `reaction:received` | Complete or cancel items via emoji reactions |

## Configuration

Plugin settings in `config.yaml`:

| Setting | Default | Description |
|---------|---------|-------------|
| `poke_interval_seconds` | `120` | How often the background loop scans for idle agents with remaining work |
| `poke_cooldown_seconds` | `300` | Minimum time between pokes for the same thread scope |
| `recent_response_grace_seconds` | `30` | Small grace period after a fresh response before auto-poking again |
| `stale_busy_seconds` | `600` | How long an agent can remain "busy" before that state is treated as stale |
| `max_pokes_per_tick` | `3` | Maximum number of poke messages sent in one scan cycle |
| `min_idle_before_poke_seconds` | `600` | Minimum idle time before a thread becomes eligible for a poke |
| `max_items_in_enrichment` | `10` | Maximum number of actionable or blocked items shown in prompt enrichment |

Example:

```yaml
plugins:
  - path: plugins/workloop
    settings:
      poke_interval_seconds: 120
      poke_cooldown_seconds: 300
      recent_response_grace_seconds: 30
      stale_busy_seconds: 600
      max_pokes_per_tick: 3
      min_idle_before_poke_seconds: 600
      max_items_in_enrichment: 10
```

## Setup

1. Copy this plugin to `~/.mindroom/plugins/workloop`.
2. Add the plugin to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/workloop
   ```
3. Add `workloop_todo_manager` to the agent's tools list.
4. Restart MindRoom.

Complements [thread-goal](https://github.com/mindroom-ai/thread-goal-plugin): thread-goal is what the agent is trying to achieve, and workloop is how it gets there.
