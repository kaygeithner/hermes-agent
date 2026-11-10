"""The adapter processing lifecycle of a producer-admitted turn (relay, webhook: ``admit_producer``).

A chat message an adapter hands to ``handle_message`` is bracketed by
``BasePlatformAdapter._process_message_background``: the typing refresh, ``on_processing_start``,
and ``on_processing_complete`` once its reply was sent. A producer delivery is only committed at
ingress (off the transport reader, acknowledged on the commit) and the authority's drain runs it, so
no adapter task brackets it. The executor does, for those admissions only (a messaging admission's
own adapter task already brackets it):

- opened when the admission starts executing, not when it commits: a delivery queued behind a
  running turn is acknowledged when it runs, as the in-band queue's are (``run_turn_followup_ack``);
- typing lives as long as execution, so it stops before the reply is sent (#117300 pauses it there);
- the outcome is reported once: an interrupted turn as CANCELLED and a failed one as FAILURE when
  execution ends; a completed one after its settlement, by the reply's send (``deliver_settled``),
  or as FAILURE when settlement dropped the reply (``drop_producer_turn``);
- a row the FIFO cannot claim (the session is paused) is answered in its chat (``tell_paused_producer``).
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from gateway.platforms.event import ProcessingOutcome

logger = logging.getLogger(__name__)
# Pauses that lift on their own: the row still runs, so its turn stays open for the answer to come.
_LIFTS_ITSELF = ('runtime_draining', 'session_busy')


def _turns(authority):
    """admission id -> ``_Queued`` (committed by a producer, not started), its open ``_Bracket``, or
    None (a test double's committed row)."""
    turns = getattr(authority, 'producer_turns', None)
    if turns is None:
        turns = authority.producer_turns = {}
    return turns


class _Queued:
    """A producer row committed and not yet executing, with the delivery event it arrived on: a pause
    answers its chat from that (the stored row may no longer restore: ``profile_mismatch``). Nothing
    to close. ``told``: 0 nothing sent, 1 told it waits (transient pause), 2 told it is paused and its
    turn closed."""
    closed = True

    def __init__(self, event):
        self.event, self.told = event, 0

    async def close(self, outcome):
        return None


def mark_producer_admission(authority, event, row):
    """``SessionAuthority._admitted``, in the commit's own tracked task: a producer's freshly queued
    row is the executor's to bracket. A re-send of a row already running or settled is not marked."""
    if getattr(event, '_producer_delivery', False) and row['status'] == 'queued':
        _turns(authority).setdefault(row['admission_id'], _Queued(event))


def tell_paused_producer(authority, ref, admission_id, reason):
    """``SessionAuthority._pause``: the FIFO stopped without claiming this row. A messaging adapter's
    waiter is released with the reason and its user told; a producer's row has none, so a relay chat
    heard nothing and the sender's turn ran into its timeout. Answer it once per state with the words
    a messaging user gets (``pause_text``), in the chat, anchored on the message. A pause that does
    not lift on its own also ends the turn (FAILURE reaction: the hub's done); one that does leaves it
    open, the row still runs. An adapter that renders no processing hooks (a webhook) has no chat."""
    entry = _turns(authority).get(admission_id)
    level = 1 if reason in _LIFTS_ITSELF else 2
    if not isinstance(entry, _Queued) or entry.told >= level:
        return
    from gateway.run_turn_followup_ack import _followup_processing_hooks_apply
    event = entry.event
    try:
        adapter = authority.runner._adapter_for_source(event.source)
        chat = _followup_processing_hooks_apply(adapter, event)
        loop = asyncio.get_running_loop()
    except Exception:  # a notice must never change how the pause itself is handled
        logger.warning('Pause notice for %s not sent', admission_id, exc_info=True)
        return
    if not chat:
        entry.told, entry.event = 2, None
        return
    entry.told = level
    if level == 2:
        entry.event = None
    task = loop.create_task(_tell(authority, adapter, event, reason))
    adapter._background_tasks.add(task)
    task.add_done_callback(adapter._background_tasks.discard)


async def _tell(authority, adapter, event, reason):
    from gateway.session_ingress import deliver_response, pause_text
    try:
        await deliver_response(adapter, event, authority.runner._session_key_for_source(event.source),
                               pause_text(event.source, reason))
    except Exception:
        logger.warning('Pause notice for %s did not reach its chat', event.source.chat_id, exc_info=True)
    if reason not in _LIFTS_ITSELF:
        await adapter._run_processing_hook('on_processing_complete', event, ProcessingOutcome.FAILURE)


class _Bracket:
    def __init__(self, adapter, event, hooks):
        from gateway.platforms.base import _reply_anchor_for_event, _thread_metadata_for_event
        from gateway.platforms.helpers import typing_metadata_for_turn
        self.adapter, self.event, self.hooks = adapter, event, hooks
        # The status anchor is this turn's: a message that reaches the chat later must not move it.
        self.metadata = typing_metadata_for_turn(
            adapter, event.source.chat_id, _reply_anchor_for_event(event), _thread_metadata_for_event(event))
        self.stop = asyncio.Event()
        self.typing = None
        self.closed = False

    async def stop_typing(self):
        task, self.typing = self.typing, None
        if task is not None:
            self.stop.set()
            await self.adapter._stop_typing_refresh(self.event.source.chat_id, task, metadata=self.metadata)

    async def close(self, outcome):
        if self.closed:
            return
        self.closed = True
        await self.stop_typing()
        if self.hooks:
            await self.adapter._run_processing_hook('on_processing_complete', self.event, outcome)


async def _open(authority, row, event):
    """The bracket of a producer-admitted row starting to execute, else None: an adapter that
    renders neither typing nor the processing hooks (a webhook) is left alone."""
    turns = _turns(authority)
    if row['admission_id'] not in turns:
        return None
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run_turn_followup_ack import _followup_processing_hooks_apply
    adapter = authority.runner._adapter_for_source(event.source)
    typing = adapter is not None and type(adapter).send_typing is not BasePlatformAdapter.send_typing
    hooks = _followup_processing_hooks_apply(adapter, event)
    if not (typing or hooks):
        turns.pop(row['admission_id'], None)
        return None
    bracket = turns[row['admission_id']] = _Bracket(adapter, event, hooks)
    if typing:
        # None for a heartbeat or ``typing_indicator: false``, as on the adapter path.
        bracket.typing = adapter._start_typing_refresh(event, bracket.stop, bracket.metadata)
    if hooks:
        try:
            await adapter._run_processing_hook('on_processing_start', event)
        except BaseException:
            # Cancelled before ``producer_turn``'s finally exists: release what was opened here.
            bracket.stop.set()
            if bracket.typing is not None:
                bracket.typing.cancel()
            turns.pop(row['admission_id'], None)
            raise
    return bracket


def _executed_outcome(captured):
    """The outcome settlement will commit (``session_results.prepare_result``), as a hook outcome."""
    result = captured.get('terminal_result', captured.get('result')) or {}
    if result.get('interrupted'):
        return ProcessingOutcome.CANCELLED
    if result.get('failed') or result.get('error'):
        return ProcessingOutcome.FAILURE
    return ProcessingOutcome.SUCCESS


@asynccontextmanager
async def producer_turn(authority, row, event, captured):
    """Around ``execute_admission``'s run of *row*: ``captured`` is its ``execution_result``."""
    bracket = await _open(authority, row, event)
    if bracket is None:
        yield
        return
    outcome = ProcessingOutcome.FAILURE
    try:
        yield
        outcome = _executed_outcome(captured)
    finally:
        await bracket.stop_typing()
        if outcome is not ProcessingOutcome.SUCCESS:
            await close_producer_turn(authority, row['admission_id'], outcome)


async def close_producer_turn(authority, admission_id, outcome):
    """Report a settled producer turn's outcome (once; a no-op for any other admission)."""
    bracket = _turns(authority).pop(admission_id, None)
    if bracket is not None:
        await bracket.close(outcome)


def drop_producer_turn(authority, admission_id):
    """The admission settled without its reply (unknown, discarded) or was cancelled before it ran:
    a started turn reports FAILURE, off the caller's path."""
    bracket = _turns(authority).pop(admission_id, None)
    if bracket is None or bracket.closed:
        return
    task = asyncio.get_running_loop().create_task(bracket.close(ProcessingOutcome.FAILURE))
    bracket.adapter._background_tasks.add(task)
    task.add_done_callback(bracket.adapter._background_tasks.discard)
