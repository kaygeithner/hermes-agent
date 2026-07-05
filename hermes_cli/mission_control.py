"""Mission Control foundation: profile-local mission events and status aggregation.

This module is intentionally command-registry/CLI/gateway agnostic.  It owns a
small profile-local event store plus a defensive, read-only status snapshot over
existing Hermes state where that state can be inspected safely.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_state import apply_wal_with_fallback

try:  # ArtifactStore is optional for status aggregation failures.
    from hermes_cli.artifacts import ArtifactRecord, ArtifactStore
except Exception:  # pragma: no cover - exercised by defensive status behavior
    ArtifactRecord = Any  # type: ignore
    ArtifactStore = None  # type: ignore

SCHEMA_VERSION = "1"
_WRITE_RETRIES = 5
_ALLOWED_EVENT_TYPES = {"note", "blocker"}
_ABSOLUTE_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~+@%=-]+){2,}(?:[^\s`)]*)")
_CORRUPT_DB_MARKERS = (
    "file is not a database",
    "database disk image is malformed",
    "file is encrypted or is not a database",
    "malformed database schema",
)


class MissionControlError(Exception):
    """Base Mission Control error."""


@dataclass(frozen=True)
class MissionEvent:
    id: str
    event_type: str
    title: str | None
    body: str | None
    source: str | None
    source_id: str | None
    session_id: str | None
    artifact_ids: list[str]
    created_at: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MissionStatus:
    goal: str | None = None
    processes: list[str] = field(default_factory=list)
    cron: list[str] = field(default_factory=list)
    kanban: list[str] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)
    events: list[MissionEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        no_goal = not self.goal or self.goal.startswith("No active goal")
        return (
            no_goal
            and not self.processes
            and not self.cron
            and not self.kanban
            and not self.artifacts
            and not self.events
        )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mission_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
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
"""


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _row_to_event(row: sqlite3.Row) -> MissionEvent:
    return MissionEvent(
        id=row["id"],
        event_type=row["event_type"],
        title=row["title"],
        body=row["body"],
        source=row["source"],
        source_id=row["source_id"],
        session_id=row["session_id"],
        artifact_ids=_json_list(row["artifact_ids"]),
        created_at=float(row["created_at"]),
        metadata=_json_dict(row["metadata"]),
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_component(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and text not in {".", ".."} and "/" not in text and "\\" not in text and not Path(text).is_absolute()


def _is_corrupt_db_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _CORRUPT_DB_MARKERS)


class MissionControl:
    def __init__(self, home: Path | None = None, session_db=None, session_id: str | None = None):
        self.home = Path(home).expanduser().resolve() if home is not None else get_hermes_home().resolve()
        self.session_db = session_db
        self.session_id = session_id
        self.root = self.home / "mission"
        self.db_path = self.root / "mission.db"
        if self.root.is_symlink():
            raise MissionControlError("Mission storage directory must not be a symlink")
        if self.db_path.is_symlink():
            raise MissionControlError("Mission database file must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._init_db_with_quarantine()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        apply_wal_with_fallback(conn, db_label="mission.db")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db_with_quarantine(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                conn.executescript(SCHEMA_SQL)
                conn.execute(
                    "INSERT OR REPLACE INTO mission_meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
                self._checkpoint(conn)
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt_db_error(exc):
                raise MissionControlError(f"Mission database could not be initialized: {exc}") from exc
            if self.db_path.exists():
                corrupt = self.db_path.with_name(f"mission.db.corrupt.{time.time_ns()}.{uuid.uuid4().hex[:8]}")
                try:
                    self.db_path.replace(corrupt)
                    for suffix in ("-wal", "-shm"):
                        sidecar = self.db_path.with_name(self.db_path.name + suffix)
                        if sidecar.exists():
                            sidecar.replace(corrupt.with_name(corrupt.name + suffix))
                except OSError as replace_exc:
                    raise MissionControlError(
                        f"Mission database is corrupt and could not be quarantined: {replace_exc}"
                    ) from exc
                with contextlib.closing(self._connect()) as conn:
                    conn.executescript(SCHEMA_SQL)
                    conn.execute(
                        "INSERT OR REPLACE INTO mission_meta(key, value) VALUES('schema_version', ?)",
                        (SCHEMA_VERSION,),
                    )
                return
            raise MissionControlError(f"Mission database could not be initialized: {exc}") from exc

    def _checkpoint(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.DatabaseError:
            pass

    def _write(self, fn):
        last_exc: Exception | None = None
        for attempt in range(_WRITE_RETRIES):
            with contextlib.closing(self._connect()) as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                    self._checkpoint(conn)
                    return result
                except sqlite3.OperationalError as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.DatabaseError:
                        pass
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    last_exc = exc
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.DatabaseError:
                        pass
                    raise
            time.sleep((0.025 * (2 ** attempt)) + random.uniform(0, 0.025))
        raise MissionControlError(f"Mission database write failed after retries: {last_exc}")

    def add_event(
        self,
        event_type: str,
        body: str,
        *,
        title: str | None = None,
        session_id: str | None = None,
        artifact_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str | None = "manual",
        source_id: str | None = None,
    ) -> MissionEvent:
        event_type = str(event_type or "").strip().lower()
        if event_type not in _ALLOWED_EVENT_TYPES:
            raise ValueError(f"Unsupported mission event type: {event_type!r}")
        body = str(body or "").strip()
        if not body:
            raise ValueError("Mission event body is empty")
        event_id = uuid.uuid4().hex[:12]
        created_at = time.time()
        data = {
            "id": event_id,
            "event_type": event_type,
            "title": title,
            "body": body,
            "source": source,
            "source_id": source_id,
            "session_id": session_id or self.session_id,
            "artifact_ids": json.dumps(list(artifact_ids or [])),
            "created_at": created_at,
            "metadata": json.dumps(dict(metadata or {}), sort_keys=True),
        }

        def insert(conn: sqlite3.Connection) -> MissionEvent:
            conn.execute(
                """
                INSERT INTO mission_events(id, event_type, title, body, source, source_id,
                  session_id, artifact_ids, created_at, metadata)
                VALUES(:id, :event_type, :title, :body, :source, :source_id,
                  :session_id, :artifact_ids, :created_at, :metadata)
                """,
                data,
            )
            row = conn.execute("SELECT * FROM mission_events WHERE id=?", (event_id,)).fetchone()
            return _row_to_event(row)

        return self._write(insert)

    def recent_events(self, limit: int = 20) -> list[MissionEvent]:
        limit = max(0, min(int(limit), 1000))
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM mission_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def status(self, *, gateway: bool = False) -> MissionStatus:
        warnings: list[str] = []
        goal = self._goal_status(warnings)
        processes = self._process_status(warnings)
        cron = self._cron_status(warnings, gateway=gateway)
        kanban = self._kanban_status(warnings, gateway=gateway)
        artifacts = self._artifact_status(warnings)
        events = self.recent_events(limit=5)
        return MissionStatus(
            goal=goal,
            processes=processes,
            cron=cron,
            kanban=kanban,
            artifacts=artifacts,
            events=events,
            warnings=warnings,
        )

    def _goal_status(self, warnings: list[str]) -> str | None:
        if not self.session_id:
            return None
        try:
            from hermes_cli.goals import GoalManager

            return GoalManager(self.session_id).status_line()
        except Exception as exc:
            warnings.append(f"Goal status unavailable: {exc}")
            return None

    def _process_status(self, warnings: list[str]) -> list[str]:
        try:
            from tools.process_registry import process_registry
        except Exception as exc:
            warnings.append(f"Background process registry unavailable: {exc}")
            return []
        # No stable public list API exists in v1; do not reach into internals.
        if any(hasattr(process_registry, name) for name in ("list", "list_sessions", "snapshot")):
            for name in ("list", "list_sessions", "snapshot"):
                method = getattr(process_registry, name, None)
                if callable(method):
                    try:
                        items = method()
                    except Exception as exc:
                        warnings.append(f"Background process status unavailable: {exc}")
                        return []
                    if not items:
                        return []
                    if isinstance(items, dict):
                        values = list(items.values())
                        count = sum(1 for item in values if not isinstance(item, dict) or item.get("status") == "running")
                    elif isinstance(items, (list, tuple, set)):
                        count = sum(1 for item in items if not isinstance(item, dict) or item.get("status") == "running")
                    else:
                        count = 1
                    if count <= 0:
                        return []
                    return [f"{count} background process(es) tracked"]
        return []

    def _cron_status(self, warnings: list[str], *, gateway: bool) -> list[str]:
        lines: list[str] = []
        jobs_file = self.home / "cron" / "jobs.json"
        try:
            jobs: list[Any] = []
            if self.home == get_hermes_home().resolve():
                try:
                    jobs_module = importlib.import_module("cron.jobs")
                    jobs = jobs_module.load_jobs()
                except Exception as exc:
                    warnings.append(f"Cron jobs unavailable: {exc}")
                    jobs = []
            elif jobs_file.exists():
                data = json.loads(jobs_file.read_text(encoding="utf-8"), strict=False)
                if isinstance(data, dict):
                    jobs = data.get("jobs", [])
                elif isinstance(data, list):
                    jobs = data
                else:
                    jobs = []
                enabled = sum(1 for job in jobs if isinstance(job, dict) and job.get("enabled", True))
                if jobs:
                    lines.append(f"{len(jobs)} cron job(s), {enabled} enabled")
            output_root = self.home / "cron" / "output"
            if output_root.exists():
                outputs: list[Path] = []
                root = output_root.resolve()
                for job_dir in output_root.iterdir():
                    if not job_dir.is_dir() or not _safe_component(job_dir.name):
                        continue
                    resolved_dir = job_dir.resolve()
                    if not _is_relative_to(resolved_dir, root):
                        continue
                    outputs.extend(path for path in resolved_dir.glob("*.md") if path.is_file())
                outputs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                if outputs:
                    latest = outputs[0]
                    age = _format_age(time.time() - latest.stat().st_mtime)
                    detail = latest.name if gateway else f"`{latest}`"
                    lines.append(f"Latest cron output: {latest.parent.name}/{detail} ({age} ago)")
        except Exception as exc:
            warnings.append(f"Cron status unavailable: {exc}")
        return lines

    def _kanban_status(self, warnings: list[str], *, gateway: bool) -> list[str]:
        try:
            board = "default"
            db_path: Path | None = None
            if self.home == get_hermes_home().resolve():
                try:
                    from hermes_cli import kanban_db

                    board = kanban_db.get_current_board()
                    db_path = kanban_db.kanban_db_path(board)
                except Exception as exc:
                    warnings.append(f"Kanban status unavailable: {exc}")
                    return []
            current = self.home / "kanban" / "current"
            if db_path is None and current.exists():
                raw = current.read_text(encoding="utf-8").strip()
                if _safe_component(raw):
                    board = raw
            if db_path is None:
                db_path = self.home / "kanban.db" if board == "default" else self.home / "kanban" / "boards" / board / "kanban.db"
            if not db_path.exists():
                return []
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
                if not rows:
                    return [f"Kanban board `{board}`: no tasks"]
                counts = {row["status"]: int(row["count"]) for row in rows}
                active_bits = [f"{status} {counts[status]}" for status in ("running", "blocked", "ready", "todo", "review") if counts.get(status)]
                summary = ", ".join(active_bits) if active_bits else f"{sum(counts.values())} task(s)"
                return [f"Kanban board `{board}`: {summary}"]
        except Exception as exc:
            warnings.append(f"Kanban status unavailable: {exc}")
            return []

    def _artifact_status(self, warnings: list[str]) -> list[Any]:
        if ArtifactStore is None:
            return []
        try:
            return ArtifactStore(home=self.home).list_artifacts(limit=5)
        except Exception as exc:
            warnings.append(f"Artifact status unavailable: {exc}")
            return []


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _redact_paths_for_gateway(text: str) -> str:
    return _ABSOLUTE_PATH_RE.sub("[local path]", text)


def format_mission_status(status: MissionStatus, *, gateway: bool = False) -> str:
    lines = ["Mission Control"]
    if status.is_empty:
        lines.append("No active mission state yet. Add context with /mission note <text> or record a blocker with /mission blocker <text>.")
    if status.goal and not status.goal.startswith("No active goal"):
        lines.extend(["", "Goal:", f"- {status.goal}"])
    if status.processes:
        lines.extend(["", "Processes:"] + [f"- {item}" for item in status.processes])
    if status.cron:
        lines.extend(["", "Cron:"] + [f"- {item}" for item in status.cron])
    if status.kanban:
        lines.extend(["", "Kanban (current board only):"] + [f"- {item}" for item in status.kanban])
    if status.artifacts:
        lines.append("")
        lines.append("Recent artifacts:")
        for record in status.artifacts:
            title = getattr(record, "display_title", None) or getattr(record, "title", None) or getattr(record, "id", "artifact")
            lines.append(f"- {getattr(record, 'id', '?')}  {title}")
    if status.events:
        lines.append("")
        lines.append("Mission notes/blockers:")
        for event in status.events:
            label = "Blocker" if event.event_type == "blocker" else "Note"
            body = (event.body or "").replace("\n", " ")
            lines.append(f"- {label}: {body}")
    if status.warnings:
        lines.append("")
        lines.append("Unavailable sources:")
        lines.extend(f"- {warning}" for warning in status.warnings[:5])
    text = "\n".join(lines)
    return _redact_paths_for_gateway(text) if gateway else text


def format_mission_recent(events: list[MissionEvent], *, gateway: bool = False) -> str:
    if not events:
        return "No mission notes or blockers yet."
    lines = ["Recent mission events:"]
    for event in events:
        label = "Blocker" if event.event_type == "blocker" else "Note"
        body = (event.body or "").replace("\n", " ")
        lines.append(f"- {event.id} {label}: {body}")
    text = "\n".join(lines)
    return _redact_paths_for_gateway(text) if gateway else text
