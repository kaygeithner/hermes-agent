from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli.artifacts import ArtifactStore
from hermes_cli.mission_control import (
    MissionControl,
    format_mission_recent,
    format_mission_status,
)


def test_empty_status_is_useful(tmp_path):
    mc = MissionControl(home=tmp_path)

    status = mc.status()
    rendered = format_mission_status(status)

    assert status.is_empty
    assert mc.db_path == tmp_path / "mission" / "mission.db"
    assert "Mission Control" in rendered
    assert "No active mission state yet" in rendered
    assert "/mission note <text>" in rendered


def test_note_and_blocker_persist_recent(tmp_path):
    first = MissionControl(home=tmp_path)
    note = first.add_event("note", "Shipped slice 4", title="Update", artifact_ids=["abc123"])
    blocker = first.add_event("blocker", "Waiting on credentials")

    second = MissionControl(home=tmp_path)
    events = second.recent_events()

    assert [event.event_type for event in events] == ["blocker", "note"]
    assert events[0].body == "Waiting on credentials"
    assert events[1].id == note.id
    assert events[1].title == "Update"
    assert events[1].artifact_ids == ["abc123"]
    rendered = format_mission_recent(events)
    assert blocker.id in rendered
    assert "Blocker: Waiting on credentials" in rendered
    assert "Note: Shipped slice 4" in rendered


def test_artifact_list_included_by_id_and_title(tmp_path):
    src = tmp_path / "report.md"
    src.write_text("mission artifact", encoding="utf-8")
    artifact = ArtifactStore(home=tmp_path).add_path(src, title="Mission Report")

    status = MissionControl(home=tmp_path).status()
    rendered = format_mission_status(status)

    assert artifact in status.artifacts
    assert artifact.id in rendered
    assert "Mission Report" in rendered


def test_cron_recent_output_summary_if_fixture_exists(tmp_path):
    out_dir = tmp_path / "cron" / "output" / "daily-job"
    out_dir.mkdir(parents=True)
    out = out_dir / "2026-06-19.md"
    out.write_text("cron output", encoding="utf-8")
    jobs = tmp_path / "cron" / "jobs.json"
    jobs.write_text('{"jobs": [{"id": "daily-job", "name": "Daily", "enabled": true}]}', encoding="utf-8")

    status = MissionControl(home=tmp_path).status()
    rendered = format_mission_status(status)

    assert any("1 cron job(s), 1 enabled" in line for line in status.cron)
    assert any("Latest cron output: daily-job/" in line for line in status.cron)
    assert str(out) in rendered


def test_gateway_formatting_has_no_bare_absolute_paths(tmp_path):
    out_dir = tmp_path / "cron" / "output" / "job"
    out_dir.mkdir(parents=True)
    out = out_dir / "out.md"
    out.write_text("cron", encoding="utf-8")
    mc = MissionControl(home=tmp_path)
    mc.add_event("note", f"Reviewed local file {tmp_path / 'secret.txt'}")

    rendered = format_mission_status(mc.status(gateway=True), gateway=True)

    assert str(tmp_path) not in rendered
    assert str(out) not in rendered
    assert "[local path]" in rendered


def test_current_kanban_board_label_and_omission_behavior(tmp_path):
    mc = MissionControl(home=tmp_path)
    assert mc.status().kanban == []

    current = tmp_path / "kanban" / "current"
    current.parent.mkdir(parents=True)
    current.write_text("projectx\n", encoding="utf-8")
    board_dir = tmp_path / "kanban" / "boards" / "projectx"
    board_dir.mkdir(parents=True)
    db = board_dir / "kanban.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, workspace_kind TEXT NOT NULL DEFAULT 'scratch')"
        )
        conn.execute(
            "INSERT INTO tasks(id, title, status, created_at) VALUES('task1', 'Run thing', 'running', 1)"
        )
        conn.execute(
            "INSERT INTO tasks(id, title, status, created_at) VALUES('task2', 'Fix thing', 'blocked', 1)"
        )

    status = MissionControl(home=tmp_path).status()
    rendered = format_mission_status(status)

    assert status.kanban == ["Kanban board `projectx`: running 1, blocked 1"]
    assert "Kanban (current board only):" in rendered
    assert "projectx" in rendered


def test_corrupt_mission_db_is_quarantined_and_reinitialized(tmp_path):
    mission_dir = tmp_path / "mission"
    mission_dir.mkdir()
    db = mission_dir / "mission.db"
    db.write_text("not sqlite", encoding="utf-8")

    mc = MissionControl(home=tmp_path)

    assert mc.recent_events() == []
    quarantined = list(mission_dir.glob("mission.db.corrupt.*"))
    assert len(quarantined) == 1
    assert db.exists()
