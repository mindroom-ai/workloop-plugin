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
- **Auto-poke** — a scheduled task periodically nudges idle agents that have unblocked work
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

4. Set up the auto-poke timer (one-time):

```
Use the schedule tool: schedule("every 5 minutes say !workloop-tick", new_thread=true)
```

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
```

### Agent tools

Agents get a `WorkloopTodoManager` toolkit with:

- `plan(tasks)` — create a work plan from a task list
- `add_todo(title, depends_on, priority)` — add a single todo
- `complete_todo(todo_id)` — mark done, auto-unblocks dependents
- `list_todos(show_all)` — view the work plan
- `update_todo(todo_id, ...)` — modify a todo

### How the auto-poke loop works

```
You:     "Build monitoring"
Agent:   [creates plan, works on step 1, completes it]
         [goes idle]

         5 minutes pass...

System:  You have unblocked work: Configure alerting
Agent:   [picks it up, works, completes]

         5 minutes pass...

System:  You have unblocked work: Write tests
Agent:   [works, completes]
         All todos done!
```

## Architecture

### Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `workloop_command` | `message:received` | Handle `!todo` commands |
| `inject_todos` | `message:enrich` | Inject checklist into agent context |
| `track_idle` | `message:after_response` | Record when agent finishes |
| `auto_poke` | `schedule:fired` | Nudge idle agents with unblocked work |
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

Tunable constants in `hooks.py`:

- `POKE_COOLDOWN_SECONDS` — minimum time between pokes (default: 300)
- `STALE_BUSY_SECONDS` — auto-prune busy entries older than this (default: 600)
- `MAX_POKES_PER_TICK` — max agents to poke per tick (default: 3)

## License

MIT
