# Implementation Plan: Artifact Library and Mission Control v1

## Goal

Build a simple, working v1 of two Hermes features:

1. Artifact Library: profile-local indexing and retrieval of useful generated files/outputs.
2. Mission Control: concise status/recent view for long-running work using existing goal, cron, process, kanban, and artifact state.

The v1 must be boring, tested, path-safe, and useful from CLI/gateway contexts without inventing a new task engine or UI.

## Non-goals

- No semantic search, embeddings, OCR, thumbnails, or dashboard UI.
- No automatic home-directory crawling.
- No cross-profile artifact federation.
- No replacement for kanban, cron, `/goal`, or background process tracking.
- No unrelated refactors of `cli.py`, `gateway/run.py`, or storage internals.
- No external publishing/pushing/deployment.

## Success criteria

- `/artifacts` empty state works.
- `/artifacts add <path>` indexes/copies an explicit file safely and computes metadata.
- `/artifacts list` shows recent artifacts compactly.
- `/artifacts show <id>` shows metadata and small text preview.
- `/artifacts scan cron` indexes cron output markdown files without duplicating them.
- `/mission status` shows goal/process/cron/kanban/artifact/notes state where available and a useful empty state otherwise.
- `/mission note <text>` and `/mission blocker <text>` persist manual mission events.
- Focused tests pass, including path traversal and corrupt/empty-state behavior.
- Gateway-facing formatting avoids bare local paths unless explicitly sending an artifact.
- Corrupt artifact/mission DB open failures degrade cleanly or quarantine/reinitialize with a clear message.

## Product/design note

See `docs/artifact-library-mission-control-v1.md`.

## Affected files

### New files

- `hermes_cli/artifacts.py`
  - Artifact store, schema, validation, add/list/show/scan helpers, formatting.
- `hermes_cli/mission_control.py`
  - Mission event store and read-only aggregator/formatter.
- `tests/hermes_cli/test_artifacts.py`
  - Store/path safety/add/list/show/cron scan tests.
- `tests/hermes_cli/test_mission_control.py`
  - Empty status, event store, formatting, source aggregation tests.

### Modified files

- `hermes_cli/commands.py`
  - Register `/artifacts` and `/mission` commands.
- `cli.py`
  - Add thin `_handle_artifacts_command()` and `_handle_mission_command()` dispatch wrappers.
- `gateway/run.py`
  - Add concrete cold-path command dispatch for `/artifacts` and `/mission`.
  - Add active-running read-only bypass for `/mission status`, `/mission recent`, `/artifacts list`, `/artifacts show`.
  - Defer `/artifacts send` in v1 unless a dedicated gateway delivery handler can call adapter validation/delivery directly.
- `tui_gateway/server.py`
  - Add explicit execution routing or tests proving the slash worker route executes `/artifacts` and `/mission`, not just catalog/completion surfacing.
- `website/docs/reference/slash-commands.md`
  - Add user-facing command docs if slash docs are manually maintained.
- Optional: `locales/en.yaml` only if gateway strings use localization keys. Prefer direct concise strings for v1 if consistent with nearby commands.

## Data/API contracts

### Artifact DB

Path: `{get_hermes_home()}/artifacts/artifacts.db`

Schema version can live in `artifact_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)` or use idempotent CREATE/MIGRATE helpers. v1 only needs initial schema.

```sql
CREATE TABLE IF NOT EXISTS artifacts (
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
CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_source ON artifacts(source, source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_source_identity ON artifacts(source, source_id, source_detail) WHERE source_id IS NOT NULL AND source_detail IS NOT NULL;
```

Artifact id: short stable random id, e.g. `uuid.uuid4().hex[:12]`. Stored blob path: `{HERMES_HOME}/artifacts/blobs/<artifact_id>/<safe_filename>`.

Public Python helpers:

- `ArtifactStore(home: Path | None = None)`
- `ArtifactStore.add_path(path, *, title=None, tags=None, source='manual', source_id=None, metadata=None, copy=True) -> ArtifactRecord`
- `ArtifactStore.list_artifacts(limit=20, source=None, tag=None) -> list[ArtifactRecord]`
- `ArtifactStore.get_artifact(id_or_prefix) -> ArtifactRecord | None`
- `ArtifactStore.resolve_artifact_id(id_or_prefix) -> ArtifactRecord | None`, raising/returning an explicit ambiguous-prefix result when more than one id matches.
- `ArtifactStore.scan_cron_outputs(limit=None) -> ScanResult`
- `format_artifact_list(records, *, gateway=False) -> str`
- `format_artifact_detail(record, *, gateway=False, include_preview=True) -> str`

Path rules:

- User-supplied source path must exist and be a regular file.
- Resolve symlinks before reading.
- Reject files larger than a conservative v1 cap, e.g. 100 MB, unless caller opts in. Do not build streaming blob ingestion in v1.
- Safe stored filename is basename sanitized to a single component. Reject or replace separators/control chars.
- `stored_path` must resolve under `{HERMES_HOME}/artifacts/blobs` before read/show/delete.
- `reference_path` is allowed only for trusted profile-owned sources such as cron outputs and must resolve under that source's safe root, e.g. `{HERMES_HOME}/cron/output/<safe_job_id>/`.
- DB parent dirs chmod 0700 where practical.
- Copy/hash from a single opened file descriptor, use `fstat`, and reject if the source is not a regular file or changes unexpectedly during copy.
- SQLite writes use `isolation_level=None`, `apply_wal_with_fallback()`, `BEGIN IMMEDIATE`, bounded jitter retry, and checkpointing patterned after `SessionDB`/kanban.
- Manual duplicate policy: allow same-content manual artifacts as separate records. Scan duplicate policy: dedupe by source identity, e.g. `cron` + job id + output filename.

Preview rules:

- Only preview text-like files and only first bounded chars, e.g. 4000 local CLI.
- Gateway previews are disabled by default. Gateway `show` displays metadata only unless an explicit preview subcommand is added later.
- Preview text must pass through `agent.redact.redact_sensitive_text(..., force=True)` before display. Artifact preview is a safety boundary and must not honor redaction opt-out config.
- Never preview `.env`, auth files, SSH keys, or obvious secret filenames even if added manually.

### Mission event DB

Path: `{get_hermes_home()}/mission/mission.db`

```sql
CREATE TABLE IF NOT EXISTS mission_events (
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
CREATE INDEX IF NOT EXISTS idx_mission_events_created_at ON mission_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mission_events_type ON mission_events(event_type, created_at DESC);
```

Public Python helpers:

- `MissionControl(home: Path | None = None, session_db=None, session_id=None)`
- `MissionControl.add_event(event_type, body, *, title=None, session_id=None, artifact_ids=None, metadata=None) -> MissionEvent`
- `MissionControl.recent_events(limit=20) -> list[MissionEvent]`
- `MissionControl.status(*, gateway=False) -> MissionStatus`
- `format_mission_status(status, *, gateway=False) -> str`
- `format_mission_recent(events, *, gateway=False) -> str`

Aggregator source contract:

- Goal: use `GoalManager.status_line()` plus `GoalManager.state`/`load_goal(session_id)`; `GoalManager.status()` does not exist.
- Processes: use `tools.process_registry.process_registry` APIs/checkpoint when safe; if no public list method exists, v1 can show a conservative “background process registry unavailable” line rather than reaching into private internals.
- Cron: use `cron.jobs.load_jobs()` and output directory metadata for recent runs.
- Kanban: inspect the current board only for v1, label the board in output, and omit cleanly if `hermes_cli.kanban_db` is unavailable or corrupt.
- Artifacts: use `ArtifactStore.list_artifacts(limit=5)`.
- Mission notes/blockers: use `MissionControl.recent_events()`.

## Command behavior

### `/artifacts`

Default: alias to `list`.

- `list [--source SOURCE] [--tag TAG] [--limit N]`
- `show <id-or-prefix>`
- `add <path> [--title TITLE] [--tag TAG]...`
- `scan cron [--limit N]`
- `send <id-or-prefix>` is out of scope for v1 unless implemented as a dedicated gateway handler that directly invokes adapter validation/delivery. Do not return raw `MEDIA:` paths as the implementation mechanism.

### `/mission`

Default: alias to `status`.

- `status`
- `recent [--limit N]`
- `note <text>`
- `blocker <text>`

Formatting:

- CLI can include paths in backticks.
- Gateway must avoid bare local paths. Use artifact ids and labels instead.

## Implementation slices

### Slice 1: Artifact storage foundation

1. Add `hermes_cli/artifacts.py` with dataclass, DB init, WAL fallback, safe filename, sha256, `add_path`, `list_artifacts`, `get_artifact`, preview, and formatting.
2. Add unit tests:
   - empty DB list
   - add text file and verify metadata/hash/stored path
   - reject directory/nonexistent path
   - reject unsafe id/path containment cases
   - ambiguous id prefix returns an explicit error, never first match
   - show preview bounded
   - duplicate source/hash behavior is deterministic
   - copy/hash uses stable file metadata and rejects source mutation where testable
   - corrupt DB open degrades cleanly or quarantines/reinitializes with a clear error
3. Run: `pytest tests/hermes_cli/test_artifacts.py -q`.

### Slice 2: Artifact command surface

1. Register `/artifacts` in `hermes_cli/commands.py`.
2. Add CLI handler in `cli.py` that delegates to `hermes_cli.artifacts`.
3. Add tests for command registry and direct handler if existing test helpers make that reasonable.
4. Run:
   - `pytest tests/hermes_cli/test_commands.py -q`
   - `pytest tests/hermes_cli/test_artifacts.py -q`

### Slice 3: Cron scan

1. Implement `ArtifactStore.scan_cron_outputs()` by walking `{HERMES_HOME}/cron/output/<job_id>/*.md` with safe job-id/path checks that mirror `cron.jobs._job_output_dir()` semantics.
2. Store source=`cron`, source_id=`job_id`, metadata with output filename/time.
3. Store cron artifacts as `reference_path` records under the cron output root unless copying is later required.
4. Avoid duplicating same cron output on repeated scan by source identity, not by sha256.
5. Add tests with temp `HERMES_HOME` fixture, including unsafe legacy job-id/path cases.
6. Run: `pytest tests/hermes_cli/test_artifacts.py -q`.

### Slice 4: Mission Control foundation

1. Add `hermes_cli/mission_control.py` with event DB and status aggregator.
2. Keep aggregation defensive and read-only. Missing optional sources should produce terse empty/omitted sections, not failures.
3. Add tests:
   - empty status
   - note/blocker persistence
   - artifact list included by id/title
   - cron recent output summary if fixture exists
   - gateway formatting has no bare absolute paths
   - current kanban board label/omission behavior
   - corrupt mission DB open degrades cleanly or quarantines/reinitializes with a clear error
4. Run: `pytest tests/hermes_cli/test_mission_control.py -q`.

### Slice 5: Mission command surface

1. Register `/mission` alias `/mc` in `hermes_cli/commands.py`.
2. Add CLI handler in `cli.py` delegating to `MissionControl`.
3. Add command registry tests.
4. Run:
   - `pytest tests/hermes_cli/test_commands.py -q`
   - `pytest tests/hermes_cli/test_mission_control.py -q`

### Slice 6: Gateway/TUI/docs integration

1. Add explicit gateway cold-path handlers for `/artifacts` and `/mission`.
2. Add active-running read-only bypass for `/mission status`, `/mission recent`, `/artifacts list`, and `/artifacts show`.
3. Add TUI execution routing or tests proving the slash worker can execute these commands, not just list/complete them.
4. Update `website/docs/reference/slash-commands.md` and/or docs if manually maintained.
5. Run targeted gateway/TUI tests if touched:
   - `pytest tests/gateway/test_unknown_command.py tests/gateway/test_command_bypass_active_session.py -q`
   - `pytest tests/test_tui_gateway_server.py -q -k 'commands_catalog or complete_slash'`

## Review gates

For each implementation slice:

1. Run focused tests.
2. Run spec compliance review using a fresh subagent with the slice requirements.
3. Run quality review using a fresh subagent on changed files/tests.
4. Fix blockers and rerun focused tests before moving on.

Final QA:

- Unit tests for new modules.
- Command registry tests.
- Manual smoke tests using real `hermes` or direct CLI helpers under a temp `HERMES_HOME`.
- Check empty-state behavior.
- Check malformed/corrupt DB behavior for artifact and mission DBs.
- Check gateway formatting contains no unintended bare local paths.
- Run `git diff --stat` and inspect diff.

## Verification commands

Initial focused commands:

```bash
pytest tests/hermes_cli/test_artifacts.py -q
pytest tests/hermes_cli/test_mission_control.py -q
pytest tests/hermes_cli/test_commands.py -q
```

If CLI/gateway touched:

```bash
pytest tests/gateway/test_unknown_command.py tests/gateway/test_command_bypass_active_session.py -q
pytest tests/test_tui_gateway_server.py -q -k 'commands_catalog or complete_slash'
```

Before final report:

```bash
git diff --stat
git diff --check
pytest tests/hermes_cli/test_artifacts.py tests/hermes_cli/test_mission_control.py tests/hermes_cli/test_commands.py -q
```

## Rollback notes

- Remove new command registry entries from `hermes_cli/commands.py`.
- Remove thin command handlers from `cli.py` and any gateway changes.
- Remove new modules/tests/docs.
- Runtime data is isolated under `{HERMES_HOME}/artifacts/` and `{HERMES_HOME}/mission/`; deleting those directories removes v1 state without touching sessions, cron jobs, kanban, or logs.

## Decisions locked before coding

1. `/artifacts send <id>` is deferred from v1. It must not be implemented in this pass.
2. Mission Control shows current-board kanban counts/recent active tasks only, not detailed kanban mutation or graph views.
3. Explicit `/artifacts add` copies into blob storage by default. Cron scan records are reference-only under the validated cron output root.
