from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest

from hermes_cli.artifacts import (
    AmbiguousArtifactPrefix,
    ArtifactError,
    ArtifactStore,
    format_artifact_detail,
    format_artifact_list,
    handle_artifacts_command,
    safe_filename,
)


def test_empty_db_list(tmp_path):
    store = ArtifactStore(home=tmp_path)

    assert store.db_path == tmp_path / "artifacts" / "artifacts.db"
    assert store.blob_root == tmp_path / "artifacts" / "blobs"
    assert store.list_artifacts() == []
    assert format_artifact_list([]) == "No artifacts yet."


def test_add_text_file_metadata_hash_and_stored_path(tmp_path):
    src = tmp_path / "hello.txt"
    body = "hello artifact\n"
    src.write_text(body, encoding="utf-8")
    store = ArtifactStore(home=tmp_path)

    record = store.add_path(src, title="Hello", tags=["one"])

    assert record.title == "Hello"
    assert record.kind == "text"
    assert record.source == "manual"
    assert record.size_bytes == len(body.encode())
    assert record.sha256 == hashlib.sha256(body.encode()).hexdigest()
    assert record.tags == ["one"]
    assert record.original_path == str(src.resolve())
    assert record.reference_path is None
    assert record.stored_path is not None
    stored = Path(record.stored_path)
    assert stored.read_text(encoding="utf-8") == body
    assert stored.resolve().is_relative_to(store.blob_root.resolve())
    assert stored.parent.name == record.id
    assert stored.name == "hello.txt"


def test_reject_directory_and_nonexistent_path(tmp_path):
    store = ArtifactStore(home=tmp_path)

    with pytest.raises(FileNotFoundError):
        store.add_path(tmp_path / "missing.txt")
    with pytest.raises(ValueError, match="regular file"):
        store.add_path(tmp_path)


def test_safe_filename_is_single_component_and_stored_path_containment(tmp_path):
    assert safe_filename("../bad/name\0.txt") == "name_.txt"
    src = tmp_path / "source.txt"
    src.write_text("ok", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE artifacts SET stored_path=? WHERE id=?",
            (str(tmp_path / "escape.txt"), record.id),
        )

    with pytest.raises(ValueError, match="Invalid artifact id"):
        store.get_artifact("../escape")
    with pytest.raises(ArtifactError, match="escapes blob root"):
        store.get_artifact(record.id)


def test_reference_path_containment(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("ok", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)
    stored = record.stored_path

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE artifacts SET stored_path=NULL, reference_path=? WHERE id=?",
            (stored, record.id),
        )

    with pytest.raises(ArtifactError, match="escapes trusted source root"):
        store.get_artifact(record.id)


def test_ambiguous_id_prefix_raises_explicit_error(tmp_path):
    store = ArtifactStore(home=tmp_path)
    now = 1.0
    with sqlite3.connect(store.db_path) as conn:
        for artifact_id in ("abc111111111", "abc222222222"):
            conn.execute(
                """
                INSERT INTO artifacts(id, kind, source, size_bytes, created_at, updated_at, tags, metadata)
                VALUES(?, 'text', 'manual', 0, ?, ?, '[]', '{}')
                """,
                (artifact_id, now, now),
            )

    with pytest.raises(AmbiguousArtifactPrefix) as excinfo:
        store.resolve_artifact_id("abc")

    assert excinfo.value.matches == ["abc111111111", "abc222222222"]


def test_preview_is_bounded_and_force_redacted(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("OPENAI_API_KEY=sk-" + "a" * 80 + "\n" + "x" * 5000, encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)

    preview = store.preview_text(record, max_chars=120)

    assert preview is not None
    assert len(preview) <= 160  # redaction can alter length slightly
    assert "sk-" not in preview
    assert "OPENAI_API_KEY=" in preview
    detail = format_artifact_detail(record, store=store)
    assert "Preview:" in detail
    assert record.stored_path is not None
    assert record.stored_path in detail
    assert "Stored path:" not in format_artifact_detail(record, gateway=True, store=store)


def test_secret_filename_is_never_previewed(tmp_path):
    src = tmp_path / ".env"
    src.write_text("TOKEN=super-secret", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)

    assert store.preview_text(record) is None


def test_preview_refuses_leaf_symlink_inside_blob_root(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("safe", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)
    assert record.stored_path is not None
    stored = Path(record.stored_path)
    alternate = stored.parent / "alternate.md"
    alternate.write_text("do not follow", encoding="utf-8")
    stored.unlink()
    try:
        stored.symlink_to(alternate)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported")

    assert store.preview_text(record) is None


def test_preview_rechecks_opened_file_under_trusted_root(monkeypatch, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("escaped", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)
    assert record.stored_path is not None
    original = Path(record.stored_path)
    original_parent = original.parent

    def swap_parent_after_validation(_record):
        shutil.rmtree(original_parent)
        original_parent.symlink_to(outside, target_is_directory=True)
        return original

    monkeypatch.setattr(store, "artifact_path", swap_parent_after_validation)

    assert store.preview_text(record) is None


def test_format_detail_without_store_has_no_preview_side_effect(monkeypatch, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("preview", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("format_artifact_detail must not construct ArtifactStore")

    monkeypatch.setattr("hermes_cli.artifacts.ArtifactStore", fail_if_constructed)

    detail = format_artifact_detail(record)

    assert "Preview:" not in detail


def test_duplicate_policy_manual_allowed_cron_source_identity_dedupes(tmp_path):
    src = tmp_path / "same.txt"
    src.write_text("same", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)

    first = store.add_path(src)
    second = store.add_path(src)
    assert first.id != second.id
    assert first.sha256 == second.sha256

    cron_root = tmp_path / "cron" / "output" / "job1"
    cron_root.mkdir(parents=True)
    out = cron_root / "run.md"
    out.write_text("cron output", encoding="utf-8")

    scan1 = store.scan_cron_outputs()
    scan2 = store.scan_cron_outputs()

    assert scan1.added == 1
    assert scan2.added == 0
    assert scan2.skipped == 1
    cron_records = store.list_artifacts(source="cron")
    assert len(cron_records) == 1
    assert cron_records[0].reference_path == str(out.resolve())
    assert cron_records[0].stored_path is None


def test_copy_hash_rejects_source_mutation(monkeypatch, tmp_path):
    src = tmp_path / "mutating.txt"
    src.write_text("before", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    real_fstat = os.fstat
    calls = {"count": 0}

    def fake_fstat(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size == len("before"):
            calls["count"] += 1
            if calls["count"] >= 2:
                values = list(st)
                values[8] = values[8] + 10  # st_mtime seconds; st_mtime_ns follows.
                return os.stat_result(values)
        return st

    monkeypatch.setattr("hermes_cli.artifacts.os.fstat", fake_fstat)

    with pytest.raises(ArtifactError, match="changed"):
        store.add_path(src)
    assert store.list_artifacts() == []


def test_blob_directory_removed_when_db_write_fails(monkeypatch, tmp_path):
    src = tmp_path / "will-fail.txt"
    src.write_text("blob", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)

    def fail_write(_fn):
        raise ArtifactError("write failed")

    monkeypatch.setattr(store, "_write", fail_write)

    with pytest.raises(ArtifactError, match="write failed"):
        store.add_path(src)

    assert not any(store.blob_root.iterdir())


def test_corrupt_db_is_quarantined_and_reinitialized(tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    db = artifacts_dir / "artifacts.db"
    db.write_text("not sqlite", encoding="utf-8")

    store = ArtifactStore(home=tmp_path)

    assert store.list_artifacts() == []
    quarantined = list(artifacts_dir.glob("artifacts.db.corrupt.*"))
    assert len(quarantined) == 1
    assert db.exists()


def test_scan_cron_outputs_skips_unsafe_and_dedupes_by_source_identity(tmp_path):
    safe = tmp_path / "cron" / "output" / "job-safe"
    safe.mkdir(parents=True)
    first = safe / "first.md"
    first.write_text("first", encoding="utf-8")
    (safe / "ignore.txt").write_text("ignore", encoding="utf-8")
    outside = tmp_path / "outside-cron"
    outside.mkdir()
    (outside / "bad.md").write_text("bad", encoding="utf-8")
    unsafe = tmp_path / "cron" / "output" / "unsafe-link"
    try:
        unsafe.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    store = ArtifactStore(home=tmp_path)

    result = store.scan_cron_outputs()
    again = store.scan_cron_outputs()

    assert result.scanned == 1
    assert result.added == 1
    assert not result.errors
    assert again.added == 0
    records = store.list_artifacts(source="cron")
    assert [r.source_id for r in records] == ["job-safe"]
    assert records[0].source_detail == "first.md"
    assert records[0].reference_path == str(first.resolve())


def test_handle_artifacts_command_add_list_show_and_scan(tmp_path):
    src = tmp_path / "command.md"
    src.write_text("command preview", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)

    added = handle_artifacts_command(f"/artifacts add {src} --title Command --tag cli", store=store)
    assert added.startswith("Added artifact ")
    listed = handle_artifacts_command("/artifacts list --tag cli", store=store)
    assert "Command" in listed
    artifact_id = store.list_artifacts()[0].id
    shown = handle_artifacts_command(f"/artifacts show {artifact_id[:6]}", store=store)
    assert "Artifact " in shown
    assert "Preview:" in shown

    cron_dir = tmp_path / "cron" / "output" / "job"
    cron_dir.mkdir(parents=True)
    (cron_dir / "out.md").write_text("cron", encoding="utf-8")
    scanned = handle_artifacts_command("/artifacts scan cron", store=store)
    assert "1 added" in scanned


def test_handle_artifacts_gateway_show_suppresses_paths_and_preview(tmp_path):
    src = tmp_path / "gateway.md"
    src.write_text("gateway preview", encoding="utf-8")
    store = ArtifactStore(home=tmp_path)
    record = store.add_path(src)

    shown = handle_artifacts_command(f"/artifacts show {record.id}", store=store, gateway=True)

    assert "Stored path:" not in shown
    assert "Preview:" not in shown


def test_handle_artifacts_send_is_deferred(tmp_path):
    store = ArtifactStore(home=tmp_path)

    assert "not implemented in v1" in handle_artifacts_command("/artifacts send abc", store=store)


def test_handle_artifacts_invalid_and_send_do_not_create_store(monkeypatch):
    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("invalid/deferred commands must not construct ArtifactStore")

    monkeypatch.setattr("hermes_cli.artifacts.ArtifactStore", fail_if_constructed)

    invalid = handle_artifacts_command("/artifacts nonsense")
    deferred = handle_artifacts_command("/artifacts send abc")

    assert "Usage:" in invalid
    assert "not implemented in v1" in deferred
