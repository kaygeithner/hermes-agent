"""A queued automation row whose own stored provenance no longer verifies must not strand the chat.

6 and 7 Nov: a background process finished while a long relay turn ran. Its completion row was
queued behind the turn with a relay seal that did not match the source it stored (the code that
ran then copied the human row's seal onto a source without that row's ``message_id``; upstream's
``91171b7f0b``, live since the 7 Nov 14:16 restart, re-seals it, and a stale seal now fails at
admission). When the turn ended the preflight refused that head with ``profile_mismatch``,
``_claim_with_retry`` paused the session, and every message Kay sent to the chat from then on went
unanswered: the head is re-checked by each drain and fails each time (2 chats, 4 messages today).
``admission_conflict`` already refuses such a head; the other verdicts on the row's own stored
provenance (``profile_mismatch``, ``invalid_params``) do the same for a row nobody waits on. A
person's row keeps the FIFO paused (they choose ``/reset``), and so does a refusal that may be
transient (``permission_denied``: the relay socket is down; ``not_found``: the adapter is swapping).

Real ``GatewayRunner`` + session authority + ``RelayAdapter`` against the fixture connector of
``test_relay_native_turn_lifecycle``; only the model call is stubbed.
"""
import asyncio
import json

import pytest

from gateway.session_results import execution_result
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.relay.test_relay_native_turn_lifecycle import (
    EYES, FAIL, OK, _actions, _gateway, _reacts, _settled, _stub_model)
from tests.waiting import async_wait_until

M1, M2, M3 = '1700000000.000100', '1700000000.000200', '1700000000.000300'
WAKE = 'a background process finished'


def _sends(outgoing, chat_id):
    return [a for a in _actions(outgoing) if a.get('op') == 'send' and a.get('chat_id') == chat_id]


def _wake_row(authority):
    rows = authority.db._read_all("SELECT status, outcome FROM session_admissions WHERE principal_id LIKE 'automation:%'")
    return [tuple(row) for row in rows]


def _stale_seal(authority):
    """The 7 Nov row: a stored relay seal that verifies over no source."""
    (row,) = authority.db._read_all("SELECT admission_id, payload_json FROM session_admissions "
                                    "WHERE principal_id LIKE 'automation:%'")
    body = json.loads(row['payload_json'])
    body['native_text_v1']['provenance']['relay'] = '0' * 64
    authority.db._write_sql('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                            (json.dumps(body, separators=(',', ':')), row['admission_id']))


async def _wake_ahead_of_kays_message(runner, authority, send, outgoing, adapter, monkeypatch, chat, *, stale_seal=False):
    """hello (a committed human turn to borrow provenance from); a long turn; a producer wake admitted
    while it runs; the long turn ends. Returns ``executed``: the wake is now the head of the chat's FIFO."""
    from gateway.session_automation import admit_route_wake
    gate = asyncio.Event()

    async def slow(_event):
        await gate.wait()
        execution_result.get()['result'] = {'final_response': 'slow done', 'messages': []}
        return 'slow done'

    executed = _stub_model(runner, monkeypatch, {'slow': slow})
    await send(M1, chat, 'hello')
    await async_wait_until(_settled(outgoing, M1, (OK, False)), timeout=20, what='the first turn')
    await send(M2, chat, 'slow', thread_id=M1)  # a reply in M1's thread: the same session
    await async_wait_until(lambda: 'slow' in executed, timeout=20, what='the long turn running')
    entry = next(e for e in runner.session_store._entries.values()
                 if e.origin is not None and e.origin.chat_id == chat)
    assert await admit_route_wake(runner, adapter, entry.origin, WAKE, 'loop') is True
    if stale_seal:
        _stale_seal(authority)
    gate.set()
    await async_wait_until(_settled(outgoing, M2, (OK, False)), timeout=20, what='the long turn ended')
    return executed


def _refusing_the_wake(monkeypatch, reason):
    """The preflight refuses the automation row only, with *reason*."""
    import gateway.session_envelope as envelope
    real = envelope.check_native_route

    async def check(runner, payload, *args, **kwargs):
        if 'automation' in payload['native_text_v1']:
            raise RuntimeStoreError(reason)
        return await real(runner, payload, *args, **kwargs)
    monkeypatch.setattr(envelope, 'check_native_route', check)


@pytest.mark.asyncio
async def test_a_completion_with_a_stale_seal_is_dropped_and_kays_next_message_is_answered(tmp_path, monkeypatch):
    async with _gateway(tmp_path, 'stale-head') as (runner, authority, send, outgoing, adapter):
        executed = await _wake_ahead_of_kays_message(
            runner, authority, send, outgoing, adapter, monkeypatch, 'S1', stale_seal=True)
        await send(M3, 'S1', 'after', thread_id=M1)
        await async_wait_until(_settled(outgoing, M3, (OK, False)), timeout=20,
                               what="Kay's message answered, not stranded behind the stale wake")
        assert executed == ['hello', 'slow', 'after']  # the stale wake never ran
        assert _wake_row(authority) == [('terminal', 'cancelled')]
        assert not [s for s in _sends(outgoing, 'S1') if 'paused' in s['content']]  # no pause: nothing to tell
        assert _reacts(outgoing, M3) == [(EYES, False), (EYES, True), (OK, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['profile_mismatch', 'invalid_params'])
async def test_an_automation_head_refused_on_its_own_provenance_is_dropped(tmp_path, monkeypatch, reason):
    async with _gateway(tmp_path, 'stale-head-' + reason) as (runner, authority, send, outgoing, adapter):
        _refusing_the_wake(monkeypatch, reason)
        executed = await _wake_ahead_of_kays_message(runner, authority, send, outgoing, adapter, monkeypatch, 'S2')
        await send(M3, 'S2', 'after', thread_id=M1)
        await async_wait_until(_settled(outgoing, M3, (OK, False)), timeout=20, what='the message behind it answered')
        assert executed == ['hello', 'slow', 'after']
        assert _wake_row(authority) == [('terminal', 'cancelled')]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['permission_denied', 'not_found'])
async def test_a_refusal_that_may_be_transient_still_pauses_and_keeps_the_wake(tmp_path, monkeypatch, reason):
    """The relay socket dropping (permission_denied) or an adapter swap (not_found) says nothing about the
    row itself: dropping a background result for it would lose it for good."""
    async with _gateway(tmp_path, 'transient-head-' + reason) as (runner, authority, send, outgoing, adapter):
        _refusing_the_wake(monkeypatch, reason)
        executed = await _wake_ahead_of_kays_message(runner, authority, send, outgoing, adapter, monkeypatch, 'S3')
        await send(M3, 'S3', 'after', thread_id=M1)
        await async_wait_until(_settled(outgoing, M3, (FAIL, False)), timeout=20, what='the pause reported')
        assert executed == ['hello', 'slow']
        assert _wake_row(authority) == [('queued', None)]


@pytest.mark.asyncio
async def test_a_persons_head_refused_on_its_provenance_keeps_the_pause(tmp_path, monkeypatch):
    """A person's own message is theirs to resolve (/reset): only automation rows are dropped."""
    async with _gateway(tmp_path, 'person-head') as (runner, authority, send, outgoing, _adapter):
        executed = _stub_model(runner, monkeypatch)

        async def refuse(*_args, **_kwargs):
            raise RuntimeStoreError('profile_mismatch')
        monkeypatch.setattr('gateway.session_envelope.check_native_route', refuse)
        await send(M1, 'S4', 'hello')
        await async_wait_until(_settled(outgoing, M1, (FAIL, False)), timeout=20, what='the pause reported')
        assert executed == []
        rows = authority.db._read_all('SELECT request_id, status, outcome FROM session_admissions')
        assert [tuple(row) for row in rows] == [(M1, 'queued', None)]
