# workloop-plugin

A [MindRoom](https://github.com/mindroom-ai/mindroom) plugin that gives agents per-thread work plans with dependencies, priority levels, and automatic idle-agent nudging.

## What it does

Without workloop: you give an agent a 5-step task, it does step 1 and waits. You poke it. It does step 2. You forget. Steps 3-5 never happen.

With workloop: the agent creates a plan, works through it, and the system keeps nudging it until every item is checked off. The plan is visible in the thread, the state is on disk, and the poke loop is automatic.

## Features

- **Per-thread todo lists** — each Matrix thread gets its own work plan stored as a JSON file
- **Dependencies** — todos can depend on other todos; blocked items are computed at read time
- **Priority levels** — critical, high, medium, low with emoji indicators
- **Agent idle detection** — tracks busy/idle state via `message:received` and `message:after_response` hooks
- **Auto-poke** — a background loop periodically nudges idle agents that have unblocked work
- **Context injection** — the `message:enrich` hook injects the thread checklist into agent context every turn
- **Reaction-based completion** — react ✅ to complete a todo, ❌ to cancel it
- **Filesystem-only storage** — JSON files with `fcntl` locks, no database, fully transparent

## Installation

1. Copy the plugin to your MindRoom plugins directory:

```bash
cp -r workloop-plugin ~/.mindroom/plugins/workloop
```

2. Add it to your `config.yaml`:

```yaml
plugins:
  - path: plugins/workloop

agents:
  my_agent:
    tools: [workloop_todo_manager]
```

3. Restart MindRoom.

## Usage

### Chat commands

```
!todo add [priority] Title [depends_on:id1,id2]
!todo list                    # Show open/actionable items
!todo all                     # Show all items including done/cancelled
!todo done <id>               # Complete a todo
!todo cancel <id>             # Cancel a todo
!todo rm <id>                 # Delete a todo permanently
!todo detail <id>             # Show full details
!todo help                    # Show usage
!workloop-tick                # Run one diagnostic poke scan now
```

### Agent tools

Agents get a `WorkloopTodoManager` toolkit with:

- `plan(tasks)` — create a work plan from a task list
- `add_todo(title, depends_on, priority)` — add a single todo
- `complete_todo(todo_id)` — mark done, auto-unblocks dependents
- `list_todos(show_all)` — view the work plan
- `update_todo(todo_id, ...)` — modify a todo

### How the auto-poke loop works

The plugin starts one router-owned background task on `agent:started`.
The first scan runs after `poke_interval_seconds`.
The loop replaces the old scheduled `!workloop-tick` heartbeat.
Manual `!workloop-tick` still works as a one-shot diagnostic command.
If you still have an old scheduled `!workloop-tick`, the plugin suppresses it and logs a deprecation warning.

```
You:     "Build monitoring"
Agent:   [creates plan, works on step 1, completes it]
         [goes idle]

         2 minutes pass...

System:  You have unblocked work: Configure alerting
Agent:   [picks it up, works, completes]

         2 minutes pass...

System:  You have unblocked work: Write tests
Agent:   [works, completes]
         All todos done!
```

## Architecture

### Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `workloop-auto-poke-start` | `agent:started` | Start the router-owned auto-poke loop |
| `workloop-auto-poke-stop` | `agent:stopped` | Stop the router-owned auto-poke loop |
| `workloop_command` | `message:received` | Handle `!todo` commands |
| `inject_todos` | `message:enrich` | Inject checklist into agent context |
| `track_idle` | `message:after_response` | Record when agent finishes |
| `auto_poke` | `schedule:fired` | Suppress deprecated scheduled `!workloop-tick` heartbeats |
| `workloop_react` | `reaction:received` | Reactions complete/cancel todos |

### Storage

```
{mindroom_data}/plugins/workloop/
  threads/{room_thread}/todos.json    # Per-thread work plans
  agents/{name}.json                  # Agent busy/idle state
```

All state is plain JSON files with `fcntl` advisory locks. No database. You can `cat` any file to see exactly what is happening.

### Idle detection

The plugin tracks agent activity via two hooks:

- `message:received` adds `room:thread` to agent `active_runs` dict
- `message:after_response` removes that entry

When `active_runs` is empty, the agent is idle. This handles concurrent messages correctly.

Stale entries (from crashes where `after_response` never fired) are auto-pruned after a configurable timeout.

## Configuration

Configure plugin settings under the plugin entry in `config.yaml`.

```yaml
plugins:
  - path: plugins/workloop
    settings:
      poke_interval_seconds: 120
      poke_cooldown_seconds: 300
      recent_response_grace_seconds: 30
      stale_busy_seconds: 600
      max_pokes_per_tick: 3
```

Available settings:

- `poke_interval_seconds` — seconds between automatic scan attempts (default: 120)
- `poke_cooldown_seconds` — minimum time between pokes for the same agent (default: 300)
- `recent_response_grace_seconds` — grace period after an agent response before it can be poked again (default: 30)
- `stale_busy_seconds` — auto-prune busy entries older than this (default: 600)
- `max_pokes_per_tick` — max agents to poke per scan (default: 3)

## License

MIT
