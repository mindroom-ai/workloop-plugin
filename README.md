# Workloop

[![License](https://img.shields.io/github/license/mindroom-ai/workloop-plugin)](https://github.com/mindroom-ai/workloop-plugin/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Autonomous, persistent work plans for [MindRoom](https://github.com/mindroom-ai/mindroom) agents.

Without workloop, an agent responds to a message and stops. With workloop, an agent breaks a task into steps, works through them one by one, and automatically resumes when it goes idle — even across multiple conversation turns. This is not a simple todo list. It's a closed-loop execution system: the agent plans, works, gets poked when idle, and keeps going until the plan is done.

## How it works

1. Agent creates a multi-step work plan with priorities and dependencies
2. Agent works through items, marking them complete as it goes
3. The plan is stored as JSON and injected into every prompt via `message:enrich`
4. When the agent finishes a response but has unfinished items, the auto-poke system nudges it to continue

## Agent tools

| Tool | Purpose |
|------|---------|
| `plan(tasks)` | Create a work plan from a multi-line task list |
| `add_todo(title, ...)` | Add a single item with optional priority and dependencies |
| `complete_todo(todo_id)` | Mark done — automatically unblocks dependent items |
| `update_todo(todo_id, ...)` | Change title, priority, status, or dependencies |
| `list_todos(show_all)` | View current plan (optionally include completed items) |

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `workloop-auto-poke-start` | `agent:started` | Start the auto-poke background loop |
| `workloop-auto-poke-stop` | `agent:stopped` | Stop the loop |
| `inject_todos` | `message:enrich` | Inject the current plan into every prompt |
| `track_idle` | `message:after_response` | Record when agent finishes responding |
| `workloop_command` | `message:received` | Handle `!todo` commands |
| `workloop_react` | `reaction:received` | Complete/cancel items via emoji reactions |

## Configuration

```yaml
# Plugin settings in config.yaml
poke_interval_seconds: 120     # How often to scan for idle agents
poke_cooldown_seconds: 300     # Min time between pokes per agent
stale_busy_seconds: 600        # Auto-prune agents stuck in "busy" state
max_pokes_per_tick: 3          # Max agents poked per scan cycle
```

## Setup

1. Copy to `~/.mindroom/plugins/workloop`
2. Add to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/workloop
   ```
3. Add `workloop` to agent's tools list
4. Restart MindRoom

Complements [thread-goal](https://github.com/mindroom-ai/thread-goal-plugin): thread-goal is *what* the agent is trying to achieve, workloop is *how* it gets there.