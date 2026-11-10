"""P7: turns the gateway starts itself (internal events, queued follow-ups) run on the chat's remembered
relay model pick — remembered per session key on human inbound, cleared by a later pick-less inbound,
surviving a restart, never leaking between chats, and still subject to the P6 eligibility check."""
import importlib
import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.relay_model_pick
from gateway.config import GatewayConfig
from gateway.relay_model_pick import recall, remember, turn_model_alias
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource
from hermes_constants import get_hermes_home
from tests.gateway.relay.test_relay_model_alias import _CFG, _GLM, _OK, _OPUS, _routes, _runner, _turn

KEY_A = "agent:main:relay:dm:chat-a"
KEY_B = "agent:main:relay:dm:chat-b"


def _human(metadata):
    return SimpleNamespace(internal=False, metadata=metadata)


def _injected():
    return SimpleNamespace(internal=True, metadata=None)


def _picks_file():
    return get_hermes_home() / "gateway" / "relay_model_picks.json"


def _picks():
    path = _picks_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_injected_turn_after_aliased_inbound_runs_on_alias():
    assert turn_model_alias(_human({"model_alias": "opus"}), KEY_A) == "opus"
    assert turn_model_alias(_injected(), KEY_A) == "opus"
    with _routes():
        agent = _turn(_runner(), turn_model_alias(_injected(), KEY_A))
    assert (agent.model, agent.provider) == _OPUS


def test_injected_turn_after_unaliased_inbound_runs_default():
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    turn_model_alias(_human({}), KEY_A)  # Kay switched the chat back to Default
    assert turn_model_alias(_injected(), KEY_A) is None
    with _routes():
        agent = _turn(_runner(), turn_model_alias(_injected(), KEY_A))
    assert (agent.model, agent.provider) == _GLM
    assert recall(KEY_A) is None
    assert KEY_A not in _picks()


def test_pick_survives_restart():
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    importlib.reload(gateway.relay_model_pick)  # fresh-process state: the in-memory cache is gone
    assert gateway.relay_model_pick.turn_model_alias(_injected(), KEY_A) == "opus"
    assert _picks() == {KEY_A: "opus"}
    with _routes():
        agent = _turn(_runner(), "opus")
    assert (agent.model, agent.provider) == _OPUS


def test_ineligible_remembered_alias_runs_default_with_warning(caplog):
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    assert turn_model_alias(_injected(), KEY_A) == "opus"
    with _routes(), caplog.at_level(logging.WARNING, logger="gateway.run"):
        agent = _turn(_runner(), "opus", cfg={"model_aliases": {}})
    assert (agent.model, agent.provider) == _GLM
    assert any("'opus'" in r.getMessage() for r in caplog.records)


def test_other_chat_alias_never_leaks():
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    assert turn_model_alias(_injected(), KEY_B) is None
    turn_model_alias(_human({"model_alias": "sonnet"}), KEY_B)
    assert turn_model_alias(_injected(), KEY_A) == "opus"
    assert turn_model_alias(_injected(), KEY_B) == "sonnet"


def test_internal_event_keeps_its_own_alias_and_never_stores():
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    assert turn_model_alias(SimpleNamespace(internal=True, metadata={"model_alias": "gpt"}), KEY_A) == "gpt"
    assert recall(KEY_A) == "opus"
    assert turn_model_alias(None, KEY_A) is None
    assert turn_model_alias(_human({"model_alias": "opus"}), None) == "opus"
    assert turn_model_alias(_human({"model_alias": "opus"}), "") == "opus"
    assert recall(KEY_A) == "opus"
    assert "" not in _picks()


def test_unchanged_pick_is_not_rewritten():
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    real = gateway.relay_model_pick.atomic_json_write
    with patch("gateway.relay_model_pick.atomic_json_write", MagicMock(wraps=real)) as write:
        turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    assert not write.called


@pytest.mark.asyncio
async def test_queued_internal_followup_uses_remembered_pick():
    """A completion wake replayed via _run_agent_queued_followup inherits the chat's pick; a queued human
    message keeps its own (absent) pick and clears the remembered one."""
    runner = object.__new__(GatewayRunner)
    runner.config, runner.adapters, runner._MAX_INTERRUPT_DEPTH = GatewayConfig(), {}, 8
    runner._run_agent = AsyncMock(return_value={"final_response": "done", "messages": []})
    runner._run_agent_deliver_first_response = runner._refresh_agent_cache_message_count = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="queued")
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value="test-session-key")
    runner._reply_anchor_for_event = runner._delivery_adapter_for = MagicMock(return_value=None)
    source = SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u")
    turn_ctx = SimpleNamespace(source=source, session_id="sid", session_key="test-session-key", run_generation=1,
                               _interrupt_depth=0, history=[], _status_thread_metadata=None, context_prompt=None,
                               result_holder=[None])
    remember("test-session-key", "opus")
    pending_event = SimpleNamespace(source=source, message_id="m2", channel_prompt=None, message_type=None,
                                    internal=True, reply_expected=None, metadata=None)
    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending="queued", pending_event=pending_event,
        response="resp", result={"messages": []}, stream_task=None)
    assert runner._run_agent.await_args.kwargs["model_alias"] == "opus"
    pending_event = SimpleNamespace(source=source, message_id="m3", channel_prompt=None, message_type=None,
                                    internal=False, reply_expected=None, metadata={})
    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending="queued", pending_event=pending_event,
        response="resp", result={"messages": []}, stream_task=None)
    kwargs = runner._run_agent.await_args.kwargs
    assert kwargs["model_alias"] is None
    assert recall("test-session-key") is None


def test_main_path_uses_turn_model_alias():
    from gateway.run_turn import GatewayTurnMixin
    assert "turn_model_alias(event, session_key)" in inspect.getsource(GatewayTurnMixin._handle_message_with_agent)


def test_goal_and_heartbeat_turns_keep_the_pick():
    turn_model_alias(_human({"model_alias": "opus"}), KEY_A)
    ev = GatewayRunner._synthetic_prompt_event(
        SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"), "continue")
    assert turn_model_alias(ev, KEY_A) == "opus"
    assert recall(KEY_A) == "opus"
    hb = SimpleNamespace(internal=False, metadata={}, _heartbeat_session_id="sid")
    assert turn_model_alias(hb, KEY_A) == "opus"
    assert recall(KEY_A) == "opus"
    assert _picks() == {KEY_A: "opus"}


def test_goal_kickoff_carries_the_users_pick():
    from gateway.platforms.event import MessageEvent
    runner = object.__new__(GatewayRunner)
    runner._adapter_and_key_for = MagicMock(return_value=(object(), "qk"))
    runner._enqueue_fifo = MagicMock()
    source = SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u")
    event = MessageEvent(text="/goal x", source=source, metadata={"model_alias": "opus"})
    GatewayRunner._enqueue_goal_turn(runner, event, "do x", label="t", kickoff=True)
    assert runner._enqueue_fifo.call_args.args[1].metadata == {"model_alias": "opus"}
    runner._enqueue_fifo.reset_mock()
    GatewayRunner._enqueue_goal_turn(runner, event, "do x", label="t", kickoff=False)
    assert runner._enqueue_fifo.call_args.args[1].metadata == {}
    event = MessageEvent(text="/goal x", source=source, metadata={})
    runner._enqueue_fifo.reset_mock()
    GatewayRunner._enqueue_goal_turn(runner, event, "do x", label="t", kickoff=True)
    assert runner._enqueue_fifo.call_args.args[1].metadata == {}


def test_transient_read_error_never_wipes_other_picks():
    remember(KEY_A, "opus")
    remember("agent:main:relay:dm:chat-c", "gpt")
    gateway.relay_model_pick._cache = (None, {})  # fresh process: the in-memory cache is gone
    with patch("pathlib.Path.read_text", side_effect=OSError(24, "Too many open files")):
        remember(KEY_B, "sonnet")  # must not raise
        assert recall(KEY_A) is None  # read failed: None, not an exception
    remember(KEY_B, "sonnet")
    assert _picks() == {KEY_A: "opus", "agent:main:relay:dm:chat-c": "gpt", KEY_B: "sonnet"}
