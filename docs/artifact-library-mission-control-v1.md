# Artifact Library and Mission Control v1

## Status

Design note after Phase 1 repository reconnaissance. No implementation yet.

## Assumptions

- v1 should be useful from CLI and messaging contexts without requiring a new UI.
- Artifact Library is profile-local by default. A generated file belongs to the Hermes profile that produced or indexed it.
- Mission Control should not duplicate Kanban's task engine. It should present and lightly link existing goal, cron, background process, and kanban state.
- Gateway responses must stay concise and must not emit bare local paths unless intentionally delivering a file.

## Success criteria

- Users can list recent useful artifacts, inspect one by id, and reference it later.
- Users can explicitly save/index a file and can see cron outputs or delivered/generated files when Hermes already knows about them.
- Users can ask for current mission status and see active goal, queued/background work, cron runs, blockers, verification, artifacts, and next action.
- Storage is simple, path-safe, profile-aware, and does not mutate user data unexpectedly.

## Repository reconnaissance summary

Relevant integration points found:

- Slash command registry: `hermes_cli/commands.py::COMMAND_REGISTRY`.
- CLI dispatch: `cli.py::HermesCLI.process_command()` and small `_handle_*_command()` wrappers.
- Goal state: `hermes_cli/goals.py::GoalManager`, stored in `SessionDB.state_meta` as `goal:<session_id>`.
- Session DB: `hermes_state.py::SessionDB`, mature for conversations, not ideal for artifact blobs.
- Profile paths: `hermes_constants.py::get_hermes_home()` for per-profile storage.
- Atomic writes: `utils.py::atomic_json_write()` and `utils.atomic_replace()`.
- Cron outputs: `cron/jobs.py`, especially `save_job_output(job_id, output)` under `{HERMES_HOME}/cron/output/{job_id}/{timestamp}.md`.
- Gateway normalized delivery: `gateway/run.py::_handle_message_with_agent()` and `gateway/platforms/base.py::BasePlatformAdapter._process_message_background()`.
- Media safety: `BasePlatformAdapter.validate_media_delivery_path()`, `extract_media()`, `extract_local_files()`.
- Background processes: `tools/process_registry.py::ProcessRegistry` and checkpoint `{HERMES_HOME}/processes.json`.
- Kanban state: `hermes_cli/kanban_db.py` already has tasks, runs, events, comments, links, attachments, and worker logs.
- Tests likely affected: `tests/hermes_cli/test_commands.py`, `tests/hermes_cli/test_goals.py`, `tests/gateway/test_*command*`, `tests/cron/*`, `tests/tools/test_terminal_tool.py`, `tests/tools/test_notify_on_complete.py`, `tests/test_tui_gateway_server.py`.

## Artifact Library v1

### Primary workflows

1. **Find recent useful outputs**
   - User runs `/artifacts` or `/artifacts list`.
   - Hermes shows a compact list: id, kind, title/name, source, age, size, and tags.

2. **Inspect one artifact**
   - User runs `/artifacts show <id>`.
   - Hermes shows metadata, source, safe stored path or reference, hash/size, and preview text for supported text artifacts.

3. **Save/index an explicit file**
   - User runs `/artifacts add <path> [--title ...] [--tag ...]`.
   - Hermes validates the path, copies or records it safely, computes sha256, and stores metadata.

4. **Surface known generated outputs**
   - v1 can index cron output files.
   - Gateway/Discord users get artifact ids and concise summaries, not noisy absolute paths.

5. **Reuse/reference later**
   - User can say “use artifact abc123” or inspect it with `/artifacts show <id>`.
   - Sending/opening artifacts is out of scope for v1 unless implemented later through a dedicated gateway delivery handler.

### v1 scope

- New artifact metadata store under `{HERMES_HOME}/artifacts/`.
- Commands:
  - `/artifacts list [--source SOURCE] [--tag TAG] [--limit N]`
  - `/artifacts show <id>`
  - `/artifacts add <path> [--title TITLE] [--tag TAG]...`
  - `/artifacts scan cron`
- Path validation and safe copy/link semantics.
- Text preview for small text/markdown/json files.
- Cron-output indexing, at minimum by explicit `/artifacts scan cron` or lazy command-time scan.
- Tests for metadata DB, path safety, CLI command output, and empty state.

### Out of scope for v1

- Full-text search over every artifact body.
- OCR, embeddings, thumbnails, semantic dedupe.
- Automatic crawling of arbitrary home directories.
- Cross-profile artifact federation.
- Deleting original source files.
- Browser UI or dashboard beyond existing command surfaces.

### Storage approach

Use a separate per-profile SQLite DB and blob/reference directory:

- DB: `{HERMES_HOME}/artifacts/artifacts.db`
- Blobs: `{HERMES_HOME}/artifacts/blobs/<artifact_id>/<safe_filename>`

Minimal table:

```sql
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT,
  description TEXT,
  source TEXT NOT NULL,
  source_id TEXT,
  source_detail TEXT,
  original_path TEXT,
  stored_path TEXT,
  reference_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]',
  metadata TEXT NOT NULL DEFAULT '{}'
);
```

Keep blobs on disk. Keep only metadata in SQLite. Use WAL with fallback via `hermes_state.apply_wal_with_fallback()`. Explicitly added files use `stored_path` under the artifact blob root; cron scan records use `reference_path` under the cron output root.

## Mission Control v1

### Primary workflows

1. **See the current mission state**
   - User runs `/mission` or `/mission status`.
   - Hermes summarizes active standing goal, subgoals, current session, active background processes, recent cron runs, kanban tasks/runs if present, blockers, verification, and next action.

2. **See recent mission history**
   - User runs `/mission recent`.
   - Hermes shows recent completed/failed/blocked runs from goal, cron, process registry, and kanban sources.

3. **Link artifacts to mission state**
   - `/mission status` includes artifact ids produced or referenced by the current/recent mission.
   - It should not dump absolute paths into Discord.

4. **Capture a manual decision/blocker/next action**
   - User runs `/mission note <text>` or `/mission blocker <text>`.
   - Hermes records a small event tied to the current session/goal.

### v1 scope

- A read-mostly Mission Control command backed by existing sources:
  - `GoalManager` state for standing goals.
  - `ProcessRegistry` for active/background terminal work.
  - cron jobs/output metadata for recent scheduled work.
  - Kanban DB for existing task/run state where available.
  - Artifact Library for artifact references.
- Optional tiny mission event store for manual notes/blockers/decisions, profile-local unless the implementation chooses to hang it off kanban events.
- Commands:
  - `/mission status`
  - `/mission recent [--limit N]`
  - `/mission note <text>`
  - `/mission blocker <text>`
- Empty state should be useful: “No active mission. Start with /goal <objective> or /background <prompt>.”

### Out of scope for v1

- A new autonomous task scheduler.
- A replacement for kanban.
- A full dashboard/TUI rebuild.
- Complex dependency graph visualization.
- Automatic inference of every decision from transcript history.
- Mutating cron, kanban, or background process state from Mission Control except explicit note/blocker events.

### Storage approach

Prefer composition over a parallel engine:

- Read existing goal state from `SessionDB.state_meta`.
- Read active/recent process state from `ProcessRegistry` checkpoint/runtime APIs.
- Read cron jobs and saved outputs from `cron/jobs.py` APIs.
- Read kanban tasks/runs/events from `hermes_cli/kanban_db.py` where available.
- Store manual mission events in a small profile-local SQLite DB only if kanban events are not a clean fit:

```sql
CREATE TABLE mission_events (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  title TEXT,
  body TEXT,
  source TEXT,
  source_id TEXT,
  session_id TEXT,
  artifact_ids TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);
```

## Command shape

Registry additions should be boring built-ins, not plugins:

- `CommandDef("artifacts", "Find and manage generated artifacts", "Session", args_hint="[list|show|add|scan] ...", subcommands=("list", "show", "add", "scan"))`
- `CommandDef("mission", "Show mission state for long-running work", "Session", aliases=("mc",), args_hint="[status|recent|note|blocker] ...", subcommands=("status", "recent", "note", "blocker"))`

Add thin handlers in CLI/gateway/TUI surfaces only where needed. Shared logic should live in small modules, not inside `cli.py` or `gateway/run.py`.

## Gateway vs local CLI behavior

- Local CLI can show safe absolute paths and “open/path” commands.
- Gateway should show artifact ids, names, source, and age. Avoid bare file paths because BasePlatformAdapter may auto-deliver files.
- Gateway should not send files in v1. A later `/artifacts send <id>` must use a dedicated gateway handler and existing media path validation, not raw `MEDIA:` text.
- Mission summaries in gateway should be one compact block, optimized for Discord/mobile.
- Streaming and already-sent responses must be considered before adding automatic artifact capture.

## Risks and mitigations

- **Path traversal / secrets:** resolve paths, reject unsafe components, reuse media delivery validators for sending, keep artifacts under profile-local root.
- **State coupling:** do not put artifact blobs in `state.db`; keep a separate small DB.
- **Duplicate mission engines:** Mission Control should aggregate Kanban/goal/cron/process state, not recreate them.
- **Gateway accidental attachments:** no bare paths in gateway summaries.
- **Crash/restart gaps:** process registry notifications are in-memory; v1 should treat them as best-effort and read checkpoint where possible.
- **Scope creep:** no semantic search/dashboard/auto-OCR in v1.

## Next implementation plan direction

1. Implement `hermes_cli/artifacts.py` with DB, path safety, add/list/show, and cron scan helpers.
2. Add `/artifacts` registry and CLI handler.
3. Implement `hermes_cli/mission_control.py` aggregator with status/recent/note/blocker.
4. Add `/mission` registry and CLI handler.
5. Add minimal gateway dispatch for read-only/status commands. Do not implement artifact sending in v1.
6. Add focused tests first for storage/path safety/formatting, then command registry/CLI behavior.
