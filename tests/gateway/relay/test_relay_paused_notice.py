"""A relay chat message that meets a paused session is answered in that chat, not dropped.

``RelayAdapter._on_inbound`` commits a chat message through ``admit_relay_delivery``; the session
authority's drain runs it. When the drain cannot claim the FIFO head it ``_pause``s the session
(``profile_mismatch`` after the v0.23.0 restart, an unknown lost turn, a refused re-authorization...).
A messaging adapter's waiter is released with the reason and its user is told (``pause_notice``). A
producer-admitted relay row has no waiter, so nothing was sent: the hub saw no reply and no done, and
timed the turn out 37 minutes later.

Real ``GatewayRunner`` + session authority + ``RelayAdapter`` against the fixture connector of
``test_relay_native_turn_lifecycle``; the preflight (``check_native_route``) is made to refuse.
"""
import asyncio
from types import SimpleNamespace

import pytest

from hermes_state_runtime import RuntimeStoreError
from tests.gateway.relay.test_relay_native_turn_lifecycle import (
    EYES, FAIL, OK, _actions, _gateway, _reacts, _settled, _stub_model)
from tests.waiting import async_wait_until

MID, MID2 = '1700000000.000100', '1700000000.000101'


def _sends(outgoing, chat_id):
    return [a for a in _actions(outgoing) if a.get('op') == 'send' and a.get('chat_id') == chat_id]


def _refusing(reason):
    async def refuse(*_args, **_kwargs):
        raise RuntimeStoreError(reason)
    return refuse


def _rows(authority, request_id):
    return authority.db._read_all(
        'SELECT status, target_session_id FROM session_admissions WHERE request_id=?', (request_id,))


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['profile_mismatch', 'permission_denied', 'not_found', 'invalid_params'])
async def test_a_message_onto_a_paused_session_gets_one_notice_and_its_turn_is_closed(tmp_path, monkeypatch, reason):
    async with _gateway(tmp_path, 'relay-paused-' + reason) as (runner, authority, send, outgoing, _adapter):
        executed = _stub_model(runner, monkeypatch)
        monkeypatch.setattr('gateway.session_envelope.check_native_route', _refusing(reason))
        await send(MID, 'P1', 'done?')
        await async_wait_until(_settled(outgoing, MID, (FAIL, False)), timeout=20,
                               what='the failure reaction that ends the hub turn')
        notices = _sends(outgoing, 'P1')
        assert len(notices) == 1, notices
        text = notices[0]['content']
        assert 'paused' in text and reason in text, text
        assert '/reset' in text, text  # the way out: /new, whose alias /reset the notice names
        # Filed under the message it answers, so the hub seals it as that turn's reply.
        assert MID in (notices[0].get('reply_to'), notices[0].get('metadata', {}).get('reply_to_message_id')), notices[0]
        assert _reacts(outgoing, MID) == [(EYES, True), (FAIL, False)]
        # Saved, not run: the pause itself is intact.
        assert executed == []
        assert [r[0] for r in _rows(authority, MID)] == ['queued']


@pytest.mark.asyncio
async def test_every_message_onto_the_pause_is_answered_once_and_never_twice(tmp_path, monkeypatch):
    async with _gateway(tmp_path, 'relay-paused-two') as (runner, authority, send, outgoing, _adapter):
        executed = _stub_model(runner, monkeypatch)
        monkeypatch.setattr('gateway.session_envelope.check_native_route', _refusing('profile_mismatch'))
        await send(MID, 'P2', 'done?')
        await async_wait_until(_settled(outgoing, MID, (FAIL, False)), timeout=20, what='the first turn closed')
        # Same session (a reply in the first message's thread): the drain pauses again, with both rows queued.
        await send(MID2, 'P2', 'hello?', thread_id=MID)
        await async_wait_until(_settled(outgoing, MID2, (FAIL, False)), timeout=20, what='the second turn closed')
        assert len(_sends(outgoing, 'P2')) == 2
        assert _reacts(outgoing, MID) == [(EYES, True), (FAIL, False)]
        assert _reacts(outgoing, MID2) == [(EYES, True), (FAIL, False)]
        assert executed == []


@pytest.mark.asyncio
async def test_an_unknown_lost_turn_notice_names_discard(tmp_path, monkeypatch):
    async with _gateway(tmp_path, 'relay-paused-unknown') as (runner, authority, send, outgoing, _adapter):
        _stub_model(runner, monkeypatch)

        async def unknown(*_args, **_kwargs):
            raise RuntimeStoreError('unknown_execution')
        monkeypatch.setattr(authority, '_claim_next', unknown)
        await send(MID, 'P3', 'done?')
        await async_wait_until(_settled(outgoing, MID, (FAIL, False)), timeout=20, what='the turn closed')
        (notice,) = _sends(outgoing, 'P3')
        # The fixture fronts Slack, which has no native /discard (its slash cap): the umbrella command.
        assert 'discard' in notice['content'] and '/reset' in notice['content'], notice


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['session_busy', 'runtime_draining'])
async def test_a_transient_pause_tells_the_chat_but_leaves_the_turn_open_for_its_answer(tmp_path, monkeypatch, reason):
    """The row still runs when the pause lifts: the notice explains the wait, no failure is reported,
    and the answer then arrives with the ordinary 👀 -> ✅ bracket."""
    async with _gateway(tmp_path, 'relay-paused-' + reason) as (runner, authority, send, outgoing, _adapter):
        executed = _stub_model(runner, monkeypatch)
        real_claim = authority._claim_next

        async def paused(ref, live):
            if reason == 'runtime_draining':
                runner._draining = True
                raise OSError('shutting down')
            return None, {'admission_id': 'ahead'}  # a worker execution holds the head
        monkeypatch.setattr(authority, '_claim_next', paused)
        await send(MID, 'P4', 'wait for me')
        await async_wait_until(lambda: _sends(outgoing, 'P4'), timeout=20, what='the notice')
        (notice,) = _sends(outgoing, 'P4')
        assert 'saved' in notice['content'] and '/reset' not in notice['content'], notice
        await asyncio.sleep(0.3)
        assert _reacts(outgoing, MID) == []  # not closed: no ❌, no 👀-remove
        assert executed == []
        runner._draining = False
        monkeypatch.setattr(authority, '_claim_next', real_claim)
        (_status, session_id), = _rows(authority, MID)
        from gateway.session_contract import SessionRef
        authority._schedule(SessionRef(authority.profile_id, session_id))
        await async_wait_until(_settled(outgoing, MID, (OK, False)), timeout=20, what='the answer once it ran')
        assert executed == ['wait for me']
        assert _reacts(outgoing, MID) == [(EYES, False), (EYES, True), (OK, False)]
        assert len(_sends(outgoing, 'P4')) == 2  # the notice, then the reply


@pytest.mark.asyncio
async def test_a_producer_with_no_chat_to_answer_in_is_left_alone():
    """The webhook adapter renders no processing hooks: its deliveries have no chat, so a pause tells nobody."""
    from gateway.session_producer_lifecycle import _Queued, tell_paused_producer

    class Webhook:
        _background_tasks = set()

    event = SimpleNamespace(source=SimpleNamespace(chat_id='hook'), message_id='m1')
    runner = SimpleNamespace(_adapter_for_source=lambda _source: Webhook())
    authority = SimpleNamespace(runner=runner, producer_turns={'adm': _Queued(event)})
    tell_paused_producer(authority, None, 'adm', 'profile_mismatch')
    assert not Webhook._background_tasks
    assert authority.producer_turns['adm'].told == 2  # decided once, not re-examined at every pause
