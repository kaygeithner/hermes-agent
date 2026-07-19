"""Oneshot teardown must forward the completed transcript."""

from unittest.mock import MagicMock

from hermes_cli import oneshot


def test_oneshot_shutdown_forwards_session_messages(monkeypatch):
    transcript = [
        {"role": "user", "content": "remember this"},
        {"role": "assistant", "content": "done"},
    ]
    agent = MagicMock()
    agent._session_messages = transcript
    agent.run_conversation.return_value = {"final_response": "ok"}

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kwargs: {})
    monkeypatch.setattr("run_agent.AIAgent", lambda **kwargs: agent)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr(oneshot, "get_fallback_chain", lambda cfg: [])

    response, _ = oneshot._run_agent("prompt", use_config_toolsets=False)

    assert response == "ok"
    agent.shutdown_memory_provider.assert_called_once_with(transcript)
    agent.close.assert_called_once_with()
