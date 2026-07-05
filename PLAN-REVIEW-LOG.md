# Plan Review Log: Artifact Library and Mission Control v1

## Round 1, Codex read-only review

Command:

```bash
codex exec -s read-only --json -o /tmp/codex-verdict-artifact-mission.txt "$(cat /tmp/codex-review-prompt.txt)"
```

Thread: `019edec6-2e82-7742-84bf-47d199387ba0`

Verdict: `REVISE`

Findings and planned fixes:

1. `GoalManager.status()` does not exist. Use `GoalManager.status_line()` plus `mgr.state` or `load_goal(session_id)`.
2. Gateway dispatch cannot stay optional. Add concrete cold-path and active-running handlers for read-only `/mission status|recent` and `/artifacts list|show`.
3. TUI has separate `slash.exec` and `command.dispatch` paths. Add execution tests and explicit routing decision.
4. Artifact/mission SQLite stores need write locking/retry, not just WAL fallback.
5. Dedupe index `(sha256, source, source_id)` is wrong for repeated cron outputs and NULL manual source ids. Use source identity `(source, source_id, source_detail)` for scans and explicit manual duplicate policy.
6. Schema conflicts between `stored_path` under blobs and reference-only cron artifacts. Split `stored_path` and `reference_path`, with validators for each.
7. TOCTOU risk in artifact add. Copy/hash from a single opened file descriptor and verify metadata.
8. Preview may leak secrets. Redact previews and disable gateway previews by default.
9. `/artifacts send` via raw `MEDIA:` is risky. Defer v1 send unless a dedicated gateway delivery handler is built.
10. Cron scan must mirror `_job_output_dir()` safe id semantics and test unsafe legacy ids.
11. Kanban board scope must be explicit. Use current board only for v1 and label it.
12. Corrupt DB behavior must be tested, not “if feasible”.

Plan revision status: applied in `PLAN.md`.

## Round 2, Codex read-only review

Command:

```bash
codex exec -s read-only --json -o /tmp/codex-verdict-artifact-mission-round2.txt "$(cat /tmp/codex-review-prompt-2.txt)"
```

Thread: `019edec9-379a-7dd2-bf00-51267906d084`

Verdict: `REVISE`

Findings and fixes applied:

1. Preview redaction must force redaction even when global config opts out. Plan now requires `agent.redact.redact_sensitive_text(..., force=True)`.
2. Design note conflicted with the plan on `/artifacts send` and `stored_path TEXT NOT NULL`. Design note now matches the revised v1: no send command, split `stored_path`/`reference_path`.
3. Prefix lookup needs ambiguous-prefix behavior. Plan now requires explicit ambiguous-prefix errors and tests.

## Round 3, Codex read-only review

Command:

```bash
codex exec -s read-only --json -o /tmp/codex-verdict-artifact-mission-round3.txt "$(cat /tmp/codex-review-prompt-3.txt)"
```

Thread: `019edecb-1cb8-7643-bdee-cf29e1e3c1e2`

Verdict: `REVISE`

Finding and fix applied:

1. Design note still said “explicit artifact sending,” and `PLAN.md` left `/artifacts send` as an open decision. Both are now locked: `/artifacts send` is deferred from v1 and must not be implemented in this pass.

## Round 4, Codex read-only review

Command:

```bash
codex exec -s read-only --json -o /tmp/codex-verdict-artifact-mission-round4.txt "$(cat /tmp/codex-review-prompt-4.txt)"
```

Thread: `019edecc-dbcb-74d2-98e6-8e24481ce2bd`

Verdict: `APPROVED`

Non-blocking note:

- TUI verification command in the plan still mentions catalog/completion, but implementation must add/run execution-specific tests if touching `slash.exec` or `command.dispatch`.
