"""SPEC-P8: a refused background result is never lost silently.

After 10 consecutive non-permanent refusals the result is posted to its chat as a
plain notice (once), the durable claim settles as delivered, and the pinned route is
never re-admitted behind the notice. A failed notice keeps the retry loop alive.
"""

import logging
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_notifications import INTERNAL_NOTIFICATION_FOOTER
from gateway.session import SessionSource


class AdmittingHandler(AsyncMock):
    """Fake transport whose successful insertion issues the production receipt."""

    async def _execute_mock_call(self, event, *args, **kwargs):
        result = await super()._execute_mock_call(event, *args, **kwargs)
        event._gateway_accepted = True
        return result


def _build_runner(monkeypatch, tmp_path, mode: str) -> GatewayRunner:
    """Create a GatewayRunner with a fake config for the given mode."""
    (tmp_path / "config.yaml").write_text(
        f"display:\n  background_process_notifications: {mode}\n",
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AdmittingHandler())
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner


def _store_origin(runner) -> None:
    runner.session_store._entries["agent:main:telegram:dm:123:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        )
    )


def _watch_event() -> dict:
    return {
        "type": "watch_match",
        "session_id": "proc_w",
        "event_id": "e1",
        "session_key": "agent:main:telegram:dm:123:42",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "42",
        "user_id": "proc_owner",
        "output": "pattern hit",
        "pattern": "ERROR",
    }


@pytest.mark.asyncio
async def test_refused_async_result_posts_notice_once_within_bound(tmp_path, monkeypatch, caplog):
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_completion_admission import pending
    from tools import async_delegation as delegation

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='guards')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    event = MessageEvent(text='human', source=source, message_id='human')
    with native_callback(runner, event, get_hermes_home()):
        await authority.admit_native(event)
    key = runner.session_store._generate_session_key(source)
    sid = runner.session_store.peek_session_id(key)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="n1"))
    monkeypatch.setattr("gateway.mirror.mirror_to_session", MagicMock(return_value=True))
    authority.db.create_session('other-owner', source='discord')
    events = [pending(key, 'held-0')]
    events[0]['parent_session_id'] = 'other-owner'
    caplog.set_level(logging.WARNING, logger="gateway.run")
    for _ in range(9):
        assert await runner._deliver_async_delegation_group(events) is False
        runner._back_off_refused_completions(events)
    adapter.send.assert_not_awaited()
    record = delegation.get_durable_delegation('held-0')
    assert (record['delivery_state'], record['delivery_attempts']) == ('pending', 0)
    # The 10th consecutive non-permanent refusal posts the result as a plain notice.
    assert await runner._deliver_async_delegation_group(events) is True
    adapter.send.assert_awaited_once()
    text = adapter.send.await_args.args[1]
    assert "refused it 10 times" in text
    assert "admission_conflict" in text
    assert 'held-0' in text
    assert INTERNAL_NOTIFICATION_FOOTER not in text
    assert delegation.get_durable_delegation('held-0')['delivery_state'] == 'delivered'
    refusal_warnings = [r.getMessage() for r in caplog.records if "held by session" in r.getMessage()]
    assert any(sid in message and 'other-owner' in message for message in refusal_warnings)
    # Once the admission frees up, the result is not re-admitted behind the notice.
    events[0]['parent_session_id'] = sid
    events[0]['_watch_retry_step'] = 0
    assert await runner._deliver_async_delegation_group(events) is not False
    adapter.send.assert_awaited_once()
    assert len(list_session_admissions(authority.db, session_id=sid, pending_only=False)) == 1


@pytest.mark.asyncio
async def test_refused_async_result_keeps_retrying_when_notice_fails(tmp_path, monkeypatch):
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_completion_admission import pending
    from tools import async_delegation as delegation

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='guards')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    event = MessageEvent(text='human', source=source, message_id='human')
    with native_callback(runner, event, get_hermes_home()):
        await authority.admit_native(event)
    key = runner.session_store._generate_session_key(source)
    sid = runner.session_store.peek_session_id(key)
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="x"))
    monkeypatch.setattr("gateway.mirror.mirror_to_session", MagicMock(return_value=True))
    authority.db.create_session('other-owner', source='discord')
    events = [pending(key, 'held-1')]
    events[0]['parent_session_id'] = 'other-owner'
    for _ in range(9):
        assert await runner._deliver_async_delegation_group(events) is False
        runner._back_off_refused_completions(events)
    # A failed notice send is not a delivery: the claim stays refunded and retryable.
    assert await runner._deliver_async_delegation_group(events) is False
    assert adapter.send.await_count == 1
    record = delegation.get_durable_delegation('held-1')
    assert (record['delivery_state'], record['delivery_attempts']) == ('pending', 0)
    runner._back_off_refused_completions(events)
    assert await runner._deliver_async_delegation_group(events) is False
    assert adapter.send.await_count == 2


@pytest.mark.asyncio
async def test_refused_watch_event_posts_notice_after_bound_and_stops(monkeypatch, tmp_path):
    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.handle_message = AsyncMock()  # never accepts: WakeNotAccepted(reason=None)
    _store_origin(runner)
    monkeypatch.setattr("gateway.run_notifications._refusal_retry_at", lambda step: 0.0)
    monkeypatch.setattr("gateway.mirror.mirror_to_session", MagicMock(return_value=True))
    completion_queue = queue.Queue()
    completion_queue.put(_watch_event())
    for _ in range(9):
        await runner._drain_watch_notifications(completion_queue)
        assert completion_queue.qsize() == 1
    adapter.send.assert_not_awaited()
    await runner._drain_watch_notifications(completion_queue)
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[0] == "123"
    text = adapter.send.await_args.args[1]
    assert "refused it 10 times" in text
    assert "not_accepted" in text
    assert completion_queue.qsize() == 0
    await runner._drain_watch_notifications(completion_queue)
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_admitted_watch_event_posts_no_notice(monkeypatch, tmp_path):
    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    _store_origin(runner)
    completion_queue = queue.Queue()
    completion_queue.put(_watch_event())
    await runner._drain_watch_notifications(completion_queue)
    adapter.handle_message.assert_awaited_once()
    assert completion_queue.qsize() == 0
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_draining_refusals_never_count_toward_the_notice(monkeypatch, tmp_path):
    from gateway.wake import WakeNotAccepted
    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    _store_origin(runner)
    monkeypatch.setattr("gateway.run_notifications._refusal_retry_at", lambda step: 0.0)
    monkeypatch.setattr("gateway.wake.admit_internal_event",
                        AsyncMock(side_effect=WakeNotAccepted("draining", reason="runtime_draining")))
    monkeypatch.setattr("gateway.mirror.mirror_to_session", MagicMock(return_value=True))
    completion_queue = queue.Queue()
    completion_queue.put(_watch_event())
    for _ in range(12):
        await runner._drain_watch_notifications(completion_queue)
    # A restart drain re-offers the result, so it never turns into a notice.
    adapter.send.assert_not_awaited()
    assert completion_queue.qsize() == 1


@pytest.mark.asyncio
async def test_refused_batch_of_two_posts_one_notice(tmp_path, monkeypatch):
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_completion_admission import pending
    from tools import async_delegation as delegation

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='guards')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    event = MessageEvent(text='human', source=source, message_id='human')
    with native_callback(runner, event, get_hermes_home()):
        await authority.admit_native(event)
    key = runner.session_store._generate_session_key(source)
    sid = runner.session_store.peek_session_id(key)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="n1"))
    monkeypatch.setattr("gateway.mirror.mirror_to_session", MagicMock(return_value=True))
    authority.db.create_session('other-owner', source='discord')
    events = [pending(key, 'batch-0'), pending(key, 'batch-1')]
    events[0]['parent_session_id'] = events[1]['parent_session_id'] = 'other-owner'
    for _ in range(9):
        assert await runner._deliver_async_delegation_group(events) is False
        runner._back_off_refused_completions(events)
    adapter.send.assert_not_awaited()
    assert await runner._deliver_async_delegation_group(events) is True
    adapter.send.assert_awaited_once()
    text = adapter.send.await_args.args[1]
    assert 'batch-0' in text and 'batch-1' in text and "refused it 10 times" in text
    assert all(delegation.get_durable_delegation(name)['delivery_state'] == 'delivered'
               for name in ('batch-0', 'batch-1'))
    for evt in events:
        evt['parent_session_id'] = sid
        evt['_watch_retry_step'] = 0
    assert await runner._deliver_async_delegation_group(events) is not False
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_refused_result_notice_is_mirrored_to_route_holder(tmp_path, monkeypatch):
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_completion_admission import pending
    from tools import async_delegation as delegation

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='guards')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    event = MessageEvent(text='human', source=source, message_id='human')
    with native_callback(runner, event, get_hermes_home()):
        await authority.admit_native(event)
    key = runner.session_store._generate_session_key(source)
    sid = runner.session_store.peek_session_id(key)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="n1"))
    mirror = MagicMock(return_value=True)
    monkeypatch.setattr("gateway.mirror.mirror_to_session", mirror)
    authority.db.create_session('other-owner', source='discord')
    events = [pending(key, 'held-m')]
    events[0]['parent_session_id'] = 'other-owner'
    for _ in range(9):
        assert await runner._deliver_async_delegation_group(events) is False
        runner._back_off_refused_completions(events)
    assert await runner._deliver_async_delegation_group(events) is True
    mirror.assert_called_once()
    assert mirror.call_args.kwargs['role'] == 'user'
    assert mirror.call_args.kwargs['session_id'] == sid
    assert mirror.call_args.kwargs['source_label'] == 'refused_result_notice'


@pytest.mark.asyncio
async def test_lost_ack_on_tenth_refusal_posts_no_notice(tmp_path, monkeypatch):
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_completion_admission import pending

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='guards')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    event = MessageEvent(text='human', source=source, message_id='human')
    with native_callback(runner, event, get_hermes_home()):
        await authority.admit_native(event)
    key = runner.session_store._generate_session_key(source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="n1"))
    monkeypatch.setattr("gateway.mirror.mirror_to_session", MagicMock(return_value=True))
    authority.db.create_session('other-owner', source='discord')
    events = [pending(key, 'held-ack')]
    events[0]['parent_session_id'] = 'other-owner'
    for _ in range(9):
        assert await runner._deliver_async_delegation_group(events) is False
        runner._back_off_refused_completions(events)
    monkeypatch.setattr(runner, '_completion_committed', lambda evt: True)
    assert await runner._deliver_async_delegation_group(events) is True
    adapter.send.assert_not_awaited()
