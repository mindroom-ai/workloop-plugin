# Workloop

MindRoom plugin that gives agents autonomous, persistent work plans.

Without workloop, an agent responds to a message and stops. With workloop, an agent can break a task into steps, work through them one by one, and automatically resume when it goes idle — even across multiple conversation turns.

## What it does

1. **Plan** — Agent creates a multi-step work plan with priorities and dependencies
2. **Execute** — Agent works through items, marking them complete as it goes
3. **Auto-poke** — When the agent finishes a response but has unfinished items, the system automatically nudges it to continue
4. **Persist** — Plans survive across turns, compaction, and restarts (stored as JSON files, injected into context via `message:enrich` hook)

This is NOT a simple todo list. It's a closed-loop execution system: the agent plans, works, gets poked when idle, and keeps going until the plan is done.

## Agent tools

| Tool | Purpose |
|------|---------|
| `plan(tasks)` | Create a work plan from a multi-line task list |
| `add_todo(title, ...)` | Add a single item with optional priority and dependencies |
| `complete_todo(todo_id)` | Mark done — automatically unblocks dependent items |
| `update_todo(todo_id, ...)` | Change title, priority, status, or dependencies |
| `list_todos(show_all)` | View current plan (optionally include completed items) |

## Auto-poke system

A background loop starts when the router agent comes online (`agent:started` hook). It periodically scans for agents that:
- Have unblocked todo items remaining
- Are not currently processing a message
- Haven't been poked recently (cooldown)

When found, the system sends a nudge message to the agent's thread, waking it up to continue working.

### Configuration

```yaml
# In plugin settings (config.yaml)
poke_interval_seconds: 120     # How often to scan for idle agents
poke_cooldown_seconds: 300     # Min time between pokes per agent
stale_busy_seconds: 600        # Auto-prune agents stuck in "busy" state
max_pokes_per_tick: 3          # Max agents poked per scan cycle
```

## User commands

- `!todo help` — Show usage
- `!workloop-tick` — Manual diagnostic poke scan (deprecated, auto-poke replaces this)

## Hooks

| Hook | Event | What it does |
|------|-------|-------------|
| `workloop-auto-poke-start` | `agent:started` | Start the auto-poke background loop |
| `workloop-auto-poke-stop` | `agent:stopped` | Stop the loop |
| `inject_todos` | `message:enrich` | Inject the current plan into every prompt |
| `track_idle` | `message:after_response` | Record when agent finishes responding |
| `workloop_command` | `message:received` | Handle `!todo` commands |
| `workloop_react` | `reaction:received` | Complete/cancel items via emoji reactions |

## Storage

Plain JSON files with `fcntl` file locking:

```
{mindroom_data}/plugins/workloop/
├── threads/{room_thread}/todos.json   # Per-thread work plans
└── agents/{name}.json                 # Agent busy/idle state
```

## Complements thread-goal

**thread-goal** = *what* the agent is trying to achieve (the destination)
**workloop** = *how* it gets there (the steps)

## Setup

1. Copy to `~/.mindroom/plugins/workloop`
2. Add to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/workloop
   ```
3. Add `workloop` to agent's tools list
4. Restart MindRoom