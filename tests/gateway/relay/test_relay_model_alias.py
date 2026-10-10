"""Per-turn ``model_alias`` over the relay (P6): wire guard, config.yaml eligibility, and ``TurnRunner.run_sync``
routing — the alias beats the session route for one turn; a failing alias keeps the session route + a notice.
run_sync tests drive the real ``_resolve_session_agent_runtime`` and agent cache (signature → evict/rebuild)."""
import logging
import threading
import types
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig
from gateway.relay.ws_transport import _relay_metadata
from gateway.run import GatewayRunner
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_runner import TurnRunner, _turn_model_alias
from gateway.session import Platform, SessionSource
from gateway.turn_context import TurnContext

_CFG = {"model_aliases": {"opus": {"model": "claude-opus-5-5", "provider": "anthropic"}}}
_SESSION_ROUTE = {"provider": "custom:spark-flash", "base_url": "http://spark", "api_key": "k", "api_mode": "chat_completions"}
_GLM = ("GLM-5.3-Flash-EXL3", "custom:spark-flash")
_OPUS = ("claude-opus-5-5", "anthropic")


class _Agent:
    _pending_fallback_notice = None  # class default: getattr works on agents that never got one

    def __init__(self, **kwargs):
        self.model, self.provider = kwargs["model"], kwargs.get("provider")
        self.session_id = kwargs.get("session_id")
        self.tools = []
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200_000, _previous_summary=None)
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.history = None

    def run_conversation(self, _message, **kw):
        self.history = kw.get("conversation_history")
        return {"final_response": "ok", "messages": []}


class _CompactingAgent(_Agent):
    def run_conversation(self, message, **kw):
        # In-place compaction on this model: summary state + the live pre-compaction list stay in memory.
        self.context_compressor._previous_summary = "opus summary"
        self._last_compaction_in_place = True
        self._session_messages = [{"role": "user", "content": f"old {i}"} for i in range(50)]
        return super().run_conversation(message, **kw)


def _runner():
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache, runner._agent_cache_lock = {}, threading.Lock()
    runner._agent_config_signature = GatewayAgentCacheMixin._agent_config_signature  # real: model change → rebuild
    runner._session_db = runner._prefill_messages = None
    runner._pending_model_notes = runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    for lane in ("_is_telegram_topic_lane", "_is_discord_auto_thread_lane", "_is_relay_discord_channel_lane"):
        getattr(runner, lane).return_value = False
    runner._resolve_session_key_or_none.return_value = "test-session-key"
    runner._peek_session_state.return_value = None  # no /model override
    runner._sessions_map.return_value = {}
    runner._resolve_session_agent_runtime = types.MethodType(GatewayTurnMixin._resolve_session_agent_runtime, runner)
    runner._resolve_turn_agent_config.side_effect = lambda _msg, model, rt: {
        "model": model, "runtime": {k: v for k, v in rt.items() if k != "request_overrides"}}
    return runner


def _turn(runner, model_alias=None, agent_cls=_Agent, history=()):
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="hi", history=list(history), session_id="sid", session_key="test-session-key", user_config=_CFG,
        model_alias=model_alias, AIAgent=agent_cls, resolve_display_setting=lambda *_a: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    assert TurnRunner(runner, ctx).run_sync()["final_response"] == "ok"
    return ctx.agent_holder[0]


@contextmanager
def _routes(session_route=_SESSION_ROUTE, alias_fails=False):
    def for_provider(provider, target_model=None):
        if alias_fails:
            raise RuntimeError("no credentials")
        return {"provider": provider, "base_url": "https://" + provider, "api_key": "k-" + provider, "api_mode": "x"}

    with patch("gateway.run._resolve_gateway_model", return_value=_GLM[0]), \
         patch("gateway.run._get_channel_override", return_value=None), \
         patch("gateway.run._load_gateway_config", return_value={}), \
         patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=lambda: dict(session_route)), \
         patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", side_effect=for_provider):
        yield


def test_wire_guard():
    assert _relay_metadata({"model_alias": " Opus "}) == {"model_alias": "opus"}
    for bad in (None, 3, {"a": 1}, ["opus"], "", "   ", "x" * 65):
        assert _relay_metadata({"model_alias": bad}) == {}
    assert _relay_metadata({"model_alias": "x" * 64}) == {"model_alias": "x" * 64}
    assert _relay_metadata({"gateway_session_key": "k", "session_key": "s", "chat_id": "c", "model_alias": "gpt"}) == {
        "model_alias": "gpt"}
    assert _relay_metadata({"reasoning_effort": "LOW", "model_alias": "gpt"}) == {"reasoning_effort": "low", "model_alias": "gpt"}
    assert _relay_metadata("opus") == {}


def test_only_plain_config_model_aliases_are_eligible():
    from hermes_cli.model_switch import DirectAlias
    ok = {"model": "claude-opus-5-5", "provider": "anthropic"}
    assert _turn_model_alias({"model_aliases": {" Opus ": ok}}, "opus") == _OPUS
    assert _turn_model_alias({"model_aliases": {"opus": ok}}, "nope") is None
    for extra in ({"base_url": "https://proxy"}, {"api_key": "k"}, {"key_env": "K"}):
        assert _turn_model_alias({"model_aliases": {"opus": {**ok, **extra}}}, "opus") is None
    assert _turn_model_alias({"model_aliases": {"opus": {"model": "m"}}}, "opus") is None
    assert _turn_model_alias({"model_aliases": {"opus": {"model": " ", "provider": "p"}}}, "opus") is None
    assert _turn_model_alias({"model_aliases": {"opus": "anthropic/claude-opus-5-5"}}, "opus") is None
    assert _turn_model_alias({"model": {"aliases": {"m": "anthropic/x"}}}, "m") is None
    with patch("hermes_cli.model_switch._BUILTIN_DIRECT_ALIASES", {"b": DirectAlias("x", "anthropic", "")}):
        assert _turn_model_alias({"model_aliases": {}}, "b") is None
    assert _turn_model_alias(None, "opus") is None


def test_alias_turn_then_session_route_turn():
    runner = _runner()
    with _routes():
        a1, a2 = _turn(runner, "opus"), _turn(runner)
    assert (a1.model, a1.provider) == _OPUS
    assert (a2.model, a2.provider) == _GLM
    # The alias lands above reasoning resolution, so per-model reasoning overrides see the turn's model.
    assert [c.kwargs["model"] for c in runner._resolve_session_reasoning_config.call_args_list] == [_OPUS[0], _GLM[0]]


def test_ineligible_alias_runs_session_route_with_warning(caplog):
    runner = _runner()
    with _routes(), caplog.at_level(logging.WARNING, logger="gateway.run"):
        agent = _turn(runner, "nope")
    assert (agent.model, agent.provider) == _GLM
    assert any("'nope'" in r.getMessage() for r in caplog.records)


def test_alias_resolution_failure_keeps_session_route_with_notice():
    runner = _runner()
    with _routes(alias_fails=True):
        agent = _turn(runner, "opus")
    assert (agent.model, agent.provider) == _GLM
    assert "anthropic/claude-opus-5-5" in agent._pending_fallback_notice
    assert "custom:spark-flash/GLM-5.3-Flash-EXL3" in agent._pending_fallback_notice


def test_eligible_alias_drops_session_route_fallback_notice():
    runner = _runner()
    with _routes(session_route={**_SESSION_ROUTE, "_fallback_notice": "⚠️ Provider fallback: stale"}):
        assert _turn(runner)._pending_fallback_notice == "⚠️ Provider fallback: stale"  # control: no alias
        agent = _turn(runner, "opus")
    assert (agent.model, agent.provider) == _OPUS
    assert agent._pending_fallback_notice is None
    assert runner._pre_agent_fallback_notice is None


def test_alternating_alias_and_default_turns():
    runner = _runner()
    agents, sigs = [], []
    with _routes():
        for alias in ("opus", None, "opus", "opus"):
            agents.append(_turn(runner, alias))
            assert list(runner._agent_cache) == ["test-session-key"]  # one entry per session, replaced on change
            sigs.append(runner._agent_cache["test-session-key"][1])
    assert [(a.model, a.provider) for a in agents] == [_OPUS, _GLM, _OPUS, _OPUS]
    assert sigs[0] != sigs[1] and sigs[0] == sigs[2] == sigs[3]
    assert len({id(a) for a in agents[:3]}) == 3  # every model change rebuilds
    assert agents[3] is agents[2]  # same pick again reuses the cached agent (warm prompt cache)


def test_rebuilt_agent_loads_post_compaction_transcript_not_evicted_state():
    runner = _runner()
    compacted = [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}, {"role": "assistant", "content": "recent"}]
    with _routes():
        opus = _turn(runner, "opus", agent_cls=_CompactingAgent)
        glm = _turn(runner, history=compacted)  # the caller reloads the persisted transcript after compaction
    assert glm is not opus and (glm.model, glm.provider) == _GLM
    assert [m["content"] for m in glm.history] == [m["content"] for m in compacted]
    assert glm.context_compressor._previous_summary is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wire, expected", [
    ({"model_alias": "Opus", "reasoning_effort": "high"}, {"model_alias": "opus", "reasoning_effort": "high"}),
    (None, {"model_alias": None, "reasoning_effort": None}),  # no metadata: session route, session effort
])
async def test_queued_relay_message_keeps_its_alias_and_effort(wire, expected):
    """A message that arrived mid-turn is replayed via _run_agent_queued_followup; its per-turn picks ride along."""
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
    pending_event = SimpleNamespace(source=source, message_id="m2", channel_prompt=None, message_type=None,
                                    internal=False, metadata=_relay_metadata(wire))
    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending="queued", pending_event=pending_event,
        response="resp", result={"messages": []}, stream_task=None)
    kwargs = runner._run_agent.await_args.kwargs
    assert {k: kwargs[k] for k in expected} == expected
