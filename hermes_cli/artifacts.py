"""Profile-local artifact storage helpers.

This module is intentionally command-agnostic.  It owns the small SQLite index
and blob/reference path safety rules used by future `/artifacts` surfaces.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import random
import re
import shlex
import shutil
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home
from hermes_state import apply_wal_with_fallback

SCHEMA_VERSION = "1"
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
PREVIEW_CHARS = 4000
_WRITE_RETRIES = 5
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_ARTIFACT_ID_PREFIX_RE = re.compile(r"[0-9a-fA-F]{1,64}")
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".csv",
    ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml",
    ".ini", ".cfg", ".conf", ".sh", ".zsh", ".bash",
}
_SECRET_NAME_MARKERS = (
    ".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "private_key",
    "authorized_keys", "known_hosts", "credentials", "secrets", "token", "apikey", "api_key",
)


class ArtifactError(Exception):
    """Base artifact-store error."""


class AmbiguousArtifactPrefix(ArtifactError, ValueError):
    """Raised when an id prefix matches more than one artifact."""

    def __init__(self, prefix: str, matches: Iterable[str]):
        self.prefix = prefix
        self.matches = sorted(matches)
        super().__init__(f"Artifact id prefix {prefix!r} is ambiguous: {', '.join(self.matches)}")


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    kind: str
    title: str | None
    description: str | None
    source: str
    source_id: str | None
    source_detail: str | None
    original_path: str | None
    stored_path: str | None
    reference_path: str | None
    mime_type: str | None
    size_bytes: int
    sha256: str | None
    created_at: float
    updated_at: float
    tags: list[str]
    metadata: dict[str, Any]

    @property
    def display_title(self) -> str:
        return self.title or self.metadata.get("filename") or self.id


@dataclass(frozen=True)
class ScanResult:
    scanned: int
    added: int
    skipped: int
    records: list[ArtifactRecord]
    errors: list[str]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifact_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_source_identity
  ON artifacts(source, source_id, source_detail)
  WHERE source_id IS NOT NULL AND source_detail IS NOT NULL;
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


def _row_to_record(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        description=row["description"],
        source=row["source"],
        source_id=row["source_id"],
        source_detail=row["source_detail"],
        original_path=row["original_path"],
        stored_path=row["stored_path"],
        reference_path=row["reference_path"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"] or 0),
        sha256=row["sha256"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        tags=_json_list(row["tags"]),
        metadata=_json_dict(row["metadata"]),
    )


def safe_filename(name: str) -> str:
    """Return a single safe path component for blob storage."""
    base = Path(str(name or "artifact")).name.strip()
    base = "".join(ch if ch.isprintable() and ch not in "/\\\0" else "_" for ch in base)
    base = _SAFE_COMPONENT_RE.sub("_", base).strip(" .")
    return base[:180] or "artifact"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _kind_for_path(path: Path, mime_type: str | None) -> str:
    if mime_type:
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("text/") or mime_type in {"application/json", "application/xml"}:
            return "text"
    return "text" if path.suffix.lower() in _TEXT_EXTENSIONS else "file"


class ArtifactStore:
    def __init__(self, home: Path | None = None):
        self.home = Path(home).expanduser().resolve() if home is not None else get_hermes_home().resolve()
        self.root = self.home / "artifacts"
        self.blob_root = self.root / "blobs"
        self.db_path = self.root / "artifacts.db"
        if self.root.is_symlink() or self.blob_root.is_symlink():
            raise ArtifactError("Artifact storage directories must not be symlinks")
        self.root.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._init_db_with_quarantine()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        apply_wal_with_fallback(conn, db_label="artifacts.db")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db_with_quarantine(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                conn.executescript(SCHEMA_SQL)
                conn.execute(
                    "INSERT OR REPLACE INTO artifact_meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
                self._checkpoint(conn)
        except sqlite3.DatabaseError as exc:
            if self.db_path.exists():
                corrupt = self.db_path.with_name(f"artifacts.db.corrupt.{int(time.time())}")
                try:
                    self.db_path.replace(corrupt)
                    for suffix in ("-wal", "-shm"):
                        sidecar = self.db_path.with_name(self.db_path.name + suffix)
                        if sidecar.exists():
                            sidecar.replace(corrupt.with_name(corrupt.name + suffix))
                except OSError as replace_exc:
                    raise ArtifactError(f"Artifact database is corrupt and could not be quarantined: {replace_exc}") from exc
                with contextlib.closing(self._connect()) as conn:
                    conn.executescript(SCHEMA_SQL)
                    conn.execute(
                        "INSERT OR REPLACE INTO artifact_meta(key, value) VALUES('schema_version', ?)",
                        (SCHEMA_VERSION,),
                    )
                return
            raise ArtifactError(f"Artifact database could not be initialized: {exc}") from exc

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
        raise ArtifactError(f"Artifact database write failed after retries: {last_exc}")

    def _validate_stored_path(self, stored_path: str | None) -> Path | None:
        if not stored_path:
            return None
        path = Path(stored_path).expanduser()
        resolved = path.resolve()
        root = self.blob_root.resolve()
        if not _is_relative_to(resolved, root):
            raise ArtifactError(f"Stored artifact path escapes blob root: {stored_path}")
        return path

    def _validate_reference_path(self, reference_path: str | None) -> Path | None:
        if not reference_path:
            return None
        path = Path(reference_path).expanduser()
        resolved = path.resolve()
        cron_root = (self.home / "cron" / "output").resolve()
        if not _is_relative_to(resolved, cron_root):
            raise ArtifactError(f"Reference artifact path escapes trusted source root: {reference_path}")
        return path

    def _record_from_row(self, row: sqlite3.Row) -> ArtifactRecord:
        record = _row_to_record(row)
        self._validate_stored_path(record.stored_path)
        self._validate_reference_path(record.reference_path)
        return record

    def add_path(
        self,
        path,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        source: str = "manual",
        source_id: str | None = None,
        source_detail: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        copy: bool = True,
        max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> ArtifactRecord:
        src = Path(path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Artifact source does not exist: {path}")
        if not src.is_file():
            raise ValueError(f"Artifact source must be a regular file: {path}")

        filename = safe_filename(src.name)
        mime_type, _ = mimetypes.guess_type(str(src))
        kind = _kind_for_path(src, mime_type)
        meta = dict(metadata or {})
        meta.setdefault("filename", filename)
        artifact_id = uuid.uuid4().hex[:12]
        now = time.time()
        stored_path: str | None = None
        reference_path: str | None = None

        fd = os.open(src, os.O_RDONLY)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"Artifact source must be a regular file: {path}")
            if before.st_size > max_bytes:
                raise ValueError(f"Artifact source exceeds {max_bytes} byte limit: {path}")
            digest = hashlib.sha256()
            if copy:
                dest_dir = self.blob_root / artifact_id
                dest_dir.mkdir(parents=True, exist_ok=False)
                dest = (dest_dir / filename).resolve()
                if not _is_relative_to(dest, self.blob_root.resolve()):
                    raise ArtifactError("Computed stored path escapes blob root")
                with os.fdopen(os.dup(fd), "rb", closefd=True) as inf, dest.open("wb") as outf:
                    while True:
                        chunk = inf.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        outf.write(chunk)
                stored_path = str(dest)
            else:
                trusted = self._validate_reference_path(str(src))
                with os.fdopen(os.dup(fd), "rb", closefd=True) as inf:
                    for chunk in iter(lambda: inf.read(1024 * 1024), b""):
                        digest.update(chunk)
                reference_path = str(trusted)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                if stored_path:
                    shutil.rmtree(Path(stored_path).parent, ignore_errors=True)
                raise ArtifactError("Artifact source changed while it was being indexed")
        finally:
            os.close(fd)

        record_data = dict(
            id=artifact_id,
            kind=kind,
            title=title,
            description=description,
            source=source,
            source_id=source_id,
            source_detail=source_detail,
            original_path=str(src),
            stored_path=stored_path,
            reference_path=reference_path,
            mime_type=mime_type,
            size_bytes=before.st_size,
            sha256=digest.hexdigest(),
            created_at=now,
            updated_at=now,
            tags=json.dumps(list(tags or [])),
            metadata=json.dumps(meta, sort_keys=True),
        )

        def insert(conn: sqlite3.Connection) -> ArtifactRecord:
            try:
                conn.execute(
                    """
                    INSERT INTO artifacts(id, kind, title, description, source, source_id, source_detail,
                      original_path, stored_path, reference_path, mime_type, size_bytes, sha256,
                      created_at, updated_at, tags, metadata)
                    VALUES(:id, :kind, :title, :description, :source, :source_id, :source_detail,
                      :original_path, :stored_path, :reference_path, :mime_type, :size_bytes, :sha256,
                      :created_at, :updated_at, :tags, :metadata)
                    """,
                    record_data,
                )
            except sqlite3.IntegrityError:
                if source_id is not None and source_detail is not None:
                    row = conn.execute(
                        "SELECT * FROM artifacts WHERE source=? AND source_id=? AND source_detail=?",
                        (source, source_id, source_detail),
                    ).fetchone()
                    if row is not None:
                        if stored_path:
                            shutil.rmtree(Path(stored_path).parent, ignore_errors=True)
                        return self._record_from_row(row)
                raise
            row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            return self._record_from_row(row)

        try:
            return self._write(insert)
        except Exception:
            if stored_path:
                shutil.rmtree(Path(stored_path).parent, ignore_errors=True)
            raise

    def list_artifacts(self, limit: int = 20, source: str | None = None, tag: str | None = None) -> list[ArtifactRecord]:
        limit = max(0, min(int(limit), 1000))
        sql = "SELECT * FROM artifacts"
        params: list[Any] = []
        where = []
        if source:
            where.append("source=?")
            params.append(source)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        records = [self._record_from_row(row) for row in rows]
        if tag:
            records = [record for record in records if tag in record.tags]
        return records

    def resolve_artifact_id(self, id_or_prefix: str) -> ArtifactRecord | None:
        prefix = str(id_or_prefix or "").strip()
        if not prefix:
            return None
        if not _ARTIFACT_ID_PREFIX_RE.fullmatch(prefix):
            raise ValueError(f"Invalid artifact id/prefix: {id_or_prefix!r}")
        with contextlib.closing(self._connect()) as conn:
            exact = conn.execute("SELECT * FROM artifacts WHERE id=?", (prefix,)).fetchone()
            if exact is not None:
                return self._record_from_row(exact)
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE id LIKE ? ORDER BY created_at DESC",
                (prefix + "%",),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousArtifactPrefix(prefix, [row["id"] for row in rows])
        return self._record_from_row(rows[0])

    def get_artifact(self, id_or_prefix: str) -> ArtifactRecord | None:
        return self.resolve_artifact_id(id_or_prefix)

    def get_by_source_identity(self, source: str, source_id: str, source_detail: str) -> ArtifactRecord | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE source=? AND source_id=? AND source_detail=?",
                (source, source_id, source_detail),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def artifact_path(self, record: ArtifactRecord) -> Path | None:
        return self._validate_stored_path(record.stored_path) or self._validate_reference_path(record.reference_path)

    def _trusted_root_for_record(self, record: ArtifactRecord) -> Path | None:
        if record.stored_path:
            return self.blob_root.resolve()
        if record.reference_path:
            return (self.home / "cron" / "output").resolve()
        return None

    def preview_text(self, record: ArtifactRecord, *, max_chars: int = PREVIEW_CHARS) -> str | None:
        path = self.artifact_path(record)
        if path is None:
            return None
        name = path.name.lower()
        if any(marker in name for marker in _SECRET_NAME_MARKERS):
            return None
        mime = record.mime_type or mimetypes.guess_type(str(path))[0]
        if not (path.suffix.lower() in _TEXT_EXTENSIONS or (mime and (mime.startswith("text/") or mime in {"application/json", "application/xml"}))):
            return None
        trusted_root = self._trusted_root_for_record(record)
        if trusted_root is None:
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            # Re-validate that the file actually opened still lives under the
            # trusted root (guards against the parent dir being swapped for a
            # symlink between validation and open). /proc is Linux-only; on
            # other platforms prove the re-resolved path is the same inode we
            # hold open, then re-check containment.
            fd_path = Path(f"/proc/self/fd/{fd}")
            if fd_path.exists():
                if not _is_relative_to(fd_path.resolve(), trusted_root):
                    return None
            else:
                try:
                    resolved = path.resolve(strict=True)
                    rst = os.stat(resolved)
                except OSError:
                    return None
                if (rst.st_dev, rst.st_ino) != (st.st_dev, st.st_ino):
                    return None
                if not _is_relative_to(resolved, trusted_root):
                    return None
            with os.fdopen(fd, "rb", closefd=True) as fh:
                fd = -1
                data = fh.read(max_chars * 4 + 1024)
        finally:
            if fd >= 0:
                os.close(fd)
        text = data.decode("utf-8", errors="replace")[:max_chars]
        return redact_sensitive_text(text, force=True)

    def scan_cron_outputs(self, limit: int | None = None) -> ScanResult:
        cron_root = (self.home / "cron" / "output").resolve()
        if not cron_root.exists():
            return ScanResult(scanned=0, added=0, skipped=0, records=[], errors=[])
        candidates: list[Path] = []
        errors: list[str] = []
        for job_dir in sorted(cron_root.iterdir()):
            job_id = job_dir.name
            try:
                if not job_dir.is_dir() or not job_id or job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
                    continue
                safe_dir = (cron_root / job_id).resolve()
                if not _is_relative_to(safe_dir, cron_root):
                    continue
                for md_path in safe_dir.glob("*.md"):
                    try:
                        md_path.stat()
                    except OSError as exc:
                        errors.append(f"{md_path}: {exc}")
                        continue
                    candidates.append(md_path)
            except OSError as exc:
                errors.append(f"{job_id}: {exc}")
        candidates_with_mtime: list[tuple[float, Path]] = []
        for candidate in candidates:
            try:
                candidates_with_mtime.append((candidate.stat().st_mtime, candidate))
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")
        candidates = [path for _mtime, path in sorted(candidates_with_mtime, key=lambda item: item[0], reverse=True)]
        if limit is not None:
            candidates = candidates[: max(0, int(limit))]
        added = skipped = 0
        records: list[ArtifactRecord] = []
        for path in candidates:
            try:
                resolved = path.resolve()
                if not _is_relative_to(resolved, cron_root) or resolved.suffix.lower() != ".md":
                    skipped += 1
                    continue
                job_id = resolved.parent.name
                existing = self.get_by_source_identity("cron", job_id, resolved.name)
                if existing is not None:
                    skipped += 1
                    records.append(existing)
                    continue
                record = self.add_path(
                    resolved,
                    title=f"Cron output {job_id}/{resolved.name}",
                    source="cron",
                    source_id=job_id,
                    source_detail=resolved.name,
                    metadata={"filename": resolved.name, "job_id": job_id, "mtime": resolved.stat().st_mtime},
                    copy=False,
                )
                added += 1
                records.append(record)
            except Exception as exc:  # scan should be best-effort
                skipped += 1
                errors.append(f"{path}: {exc}")
        return ScanResult(scanned=len(candidates), added=added, skipped=skipped, records=records, errors=errors)


def format_artifact_list(records: list[ArtifactRecord], *, gateway: bool = False) -> str:
    if not records:
        return "No artifacts yet."
    lines = ["Artifacts:"]
    for record in records:
        title = record.display_title
        source = f"{record.source}:{record.source_id}" if record.source_id else record.source
        lines.append(f"- {record.id}  {title}  ({record.kind}, {record.size_bytes} bytes, {source})")
    return "\n".join(lines)


def format_artifact_detail(record: ArtifactRecord, *, gateway: bool = False, include_preview: bool = True, store: ArtifactStore | None = None) -> str:
    lines = [
        f"Artifact {record.id}",
        f"Title: {record.display_title}",
        f"Kind: {record.kind}",
        f"Size: {record.size_bytes} bytes",
        f"Source: {record.source}" + (f":{record.source_id}" if record.source_id else ""),
    ]
    if record.sha256:
        lines.append(f"SHA256: {record.sha256}")
    if record.tags:
        lines.append("Tags: " + ", ".join(record.tags))
    if not gateway:
        if record.stored_path:
            lines.append(f"Stored path: `{record.stored_path}`")
        if record.reference_path:
            lines.append(f"Reference path: `{record.reference_path}`")
    if include_preview and not gateway and store is not None:
        preview = store.preview_text(record)
        if preview:
            lines.append("Preview:\n" + preview)
    return "\n".join(lines)


def _parse_limit(raw: str | None, default: int = 20) -> int:
    if raw is None:
        return default
    try:
        return max(0, min(int(raw), 1000))
    except ValueError as exc:
        raise ValueError(f"Invalid limit: {raw!r}") from exc


def handle_artifacts_command(command: str, *, store: ArtifactStore | None = None, gateway: bool = False) -> str:
    """Handle a `/artifacts` command and return display text.

    This shared parser is deliberately small and side-effect obvious. It does
    not implement artifact sending in v1.
    """
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return f"Invalid /artifacts command: {exc}"
    if parts and parts[0] == "/artifacts":
        parts = parts[1:]
    subcmd = parts[0].lower() if parts else "list"
    args = parts[1:] if parts else []

    if subcmd in {"list", "ls"}:
        store = store or ArtifactStore()
        limit = 20
        source = None
        tag = None
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--source" and i + 1 < len(args):
                source = args[i + 1]
                i += 2
            elif arg == "--tag" and i + 1 < len(args):
                tag = args[i + 1]
                i += 2
            elif arg == "--limit" and i + 1 < len(args):
                try:
                    limit = _parse_limit(args[i + 1])
                except ValueError as exc:
                    return str(exc)
                i += 2
            else:
                return "Usage: /artifacts list [--source SOURCE] [--tag TAG] [--limit N]"
        return format_artifact_list(store.list_artifacts(limit=limit, source=source, tag=tag), gateway=gateway)

    if subcmd == "show":
        store = store or ArtifactStore()
        if not args:
            return "Usage: /artifacts show <id>"
        try:
            record = store.get_artifact(args[0])
        except AmbiguousArtifactPrefix as exc:
            return str(exc)
        except (ArtifactError, ValueError) as exc:
            return f"Artifact error: {exc}"
        if record is None:
            return f"Artifact not found: {args[0]}"
        return format_artifact_detail(record, gateway=gateway, include_preview=not gateway, store=store)

    if subcmd == "add":
        store = store or ArtifactStore()
        if not args:
            return "Usage: /artifacts add <path> [--title TITLE] [--tag TAG]..."
        path = args[0]
        title = None
        tags: list[str] = []
        i = 1
        while i < len(args):
            arg = args[i]
            if arg == "--title" and i + 1 < len(args):
                title = args[i + 1]
                i += 2
            elif arg == "--tag" and i + 1 < len(args):
                tags.append(args[i + 1])
                i += 2
            else:
                return "Usage: /artifacts add <path> [--title TITLE] [--tag TAG]..."
        try:
            record = store.add_path(path, title=title, tags=tags)
        except Exception as exc:
            return f"Artifact add failed: {exc}"
        return f"Added artifact {record.id}: {record.display_title} ({record.kind}, {record.size_bytes} bytes)"

    if subcmd == "scan":
        store = store or ArtifactStore()
        if not args or args[0].lower() != "cron":
            return "Usage: /artifacts scan cron [--limit N]"
        limit = None
        rest = args[1:]
        if rest:
            if len(rest) == 2 and rest[0] == "--limit":
                try:
                    limit = _parse_limit(rest[1])
                except ValueError as exc:
                    return str(exc)
            else:
                return "Usage: /artifacts scan cron [--limit N]"
        result = store.scan_cron_outputs(limit=limit)
        lines = [f"Scanned cron outputs: {result.added} added, {result.skipped} skipped, {result.scanned} scanned."]
        if result.errors:
            lines.append("Errors: " + "; ".join(result.errors[:3]))
        return "\n".join(lines)

    if subcmd == "send":
        return "/artifacts send is not implemented in v1. Use /artifacts show <id> for metadata."

    return "Usage: /artifacts [list|show|add|scan] ..."
