"""Tests for the cronjob tool schema shape.

Guards the description text that flags ``schedule`` (and ``prompt``) as
REQUIRED for ``action=create`` — the load-bearing fix for description-driven
models (e.g. Grok) that omit schedule when the schema only lists ``action``
in ``required[]``. See issue #32427 / PR #32448.
"""

from __future__ import annotations


def test_cronjob_schema_action_description_flags_create_requirements():
    """`action` description must state schedule + prompt are required for create."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    action_desc = CRONJOB_SCHEMA["parameters"]["properties"]["action"]["description"]
    assert "action=create" in action_desc
    assert "schedule" in action_desc
    assert "REQUIRED" in action_desc


def test_cronjob_schema_does_not_expose_memory_write_privilege():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    assert "allow_memory_writes" not in CRONJOB_SCHEMA["parameters"]["properties"]


def test_registered_handler_drops_model_supplied_memory_privilege(monkeypatch):
    import tools.cronjob_tools as cron_tools

    monkeypatch.setattr(cron_tools, "cronjob", lambda **kwargs: kwargs)
    entry = cron_tools.registry.get_entry("cronjob")
    assert entry is not None

    result = entry.handler(
        {
            "action": "create",
            "allow_memory_writes": True,
            "attach_to_session": True,
        }
    )

    assert "allow_memory_writes" not in result
    assert result["attach_to_session"] is True


