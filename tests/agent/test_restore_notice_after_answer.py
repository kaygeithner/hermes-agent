"""'✅ Primary model restored' says the primary works again, so it is said once the primary has answered.

``restore_primary_runtime`` puts the primary back at the start of every turn, before anything is known
about it. Announcing it there told Kay "restored ... fallback is no longer active" on each turn of an
outage, a moment before the same turn's failing call switched to the fallback again and said so
("⚠️ Model fallback"): both bubbles, every turn. The restore notice now waits for the turn's final
response (the point where the pending fallback notice is emitted) and is dropped when the primary
fails again first.
"""
from unittest.mock import MagicMock, patch

from tests.agent.test_non_chat_primary_restore import _restore_after_fallback


def _restored(notices):
    return [n for n in notices if "Primary model restored" in n]


def test_restoring_the_primary_says_nothing_until_it_has_answered():
    agent, restored, emitted = _restore_after_fallback("qwen3.7-plus")

    assert restored is True and agent._fallback_activated is False
    assert _restored(emitted) == []  # an unverified claim

    agent._emit_pending_fallback_notice()  # the turn's final response, answered by the primary
    (notice,) = _restored(emitted)
    assert agent.model in notice and "glm-5.2" in notice  # the primary is back; the fallback it replaces

    agent._emit_pending_fallback_notice()
    assert len(_restored(emitted)) == 1  # one-shot


def test_a_primary_that_fails_again_never_gets_the_all_clear():
    agent, restored, emitted = _restore_after_fallback("qwen3.7-plus")
    assert restored is True

    fb_client = MagicMock()
    fb_client.api_key = "fallback-" + "key-1234"
    fb_client.base_url = "https://fallback.example.com/v1"
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, None)):
        assert agent._try_activate_fallback() is True  # the primary's first call failed again
    agent._emit_pending_fallback_notice()  # the fallback answered

    assert _restored(emitted) == []
    assert any("Model fallback" in n and "glm-5.2" in n for n in emitted), emitted


def test_a_failed_turn_drops_the_restore_notice_too():
    agent, restored, emitted = _restore_after_fallback("qwen3.7-plus")
    assert restored is True
    agent._flush_status_buffer()  # terminal failure of the turn
    agent._emit_pending_fallback_notice()
    assert _restored(emitted) == []
