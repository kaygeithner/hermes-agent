"""Tests for the mem0 dead-letter queue and first-turn prefetch."""

import json
import time

from plugins.memory.mem0 import Mem0MemoryProvider, _DEADLETTER_MAX


class FakeBackend:
    def __init__(self, fail=False):
        self.fail = fail
        self.added = []
        self.search_results = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        if self.fail:
            raise ConnectionError("spark down")
        self.added.append(messages)
        return {}

    def search(self, query, *, filters, top_k=10, rerank=True):
        return self.search_results


def _provider(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    p = Mem0MemoryProvider()
    p._backend = backend
    p._config = {"mode": "oss", "oss": {}}
    p._mode = "oss"
    return p


def _sync_and_join(p, user, asst):
    p.sync_turn(user, asst)
    t = p._sync_thread
    if t:
        t.join(timeout=10)


def _deadletter(tmp_path):
    return tmp_path / "state" / "mem0-deadletter.jsonl"


def test_failed_sync_lands_in_deadletter(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, FakeBackend(fail=True))
    _sync_and_join(p, "hello", "world")
    lines = _deadletter(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["messages"][0]["content"] == "hello"
    assert entry["messages"][1]["content"] == "world"


def test_successful_sync_replays_deadletter(tmp_path, monkeypatch):
    backend = FakeBackend(fail=True)
    p = _provider(tmp_path, monkeypatch, backend)
    _sync_and_join(p, "lost1", "a1")
    _sync_and_join(p, "lost2", "a2")
    backend.fail = False
    _sync_and_join(p, "live", "a3")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", "lost1", "lost2"]
    assert not _deadletter(tmp_path).exists()


def test_breaker_open_turn_is_queued_not_dropped(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._consecutive_failures = 5
    p._breaker_open_until = time.monotonic() + 100
    p.sync_turn("dropped", "x")
    assert backend.added == []
    lines = _deadletter(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["messages"][0]["content"] == "dropped"


def test_deadletter_is_bounded(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, FakeBackend())
    for i in range(_DEADLETTER_MAX + 20):
        p._deadletter_append(f"u{i}", "a")
    lines = _deadletter(tmp_path).read_text().splitlines()
    assert len(lines) == _DEADLETTER_MAX
    # oldest dropped, newest kept
    assert json.loads(lines[-1])["messages"][0]["content"] == f"u{_DEADLETTER_MAX + 19}"


def test_corrupt_deadletter_line_is_dropped(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("good", "turn")
    path = _deadletter(tmp_path)
    path.write_text("garbage not json\n" + path.read_text())
    _sync_and_join(p, "live", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", "good"]
    assert not path.exists()


def test_first_turn_prefetch(tmp_path, monkeypatch):
    backend = FakeBackend()
    backend.search_results = [{"memory": "Kay likes tea"}]
    p = _provider(tmp_path, monkeypatch, backend)
    p.on_turn_start(1, "hello")
    out = p.prefetch("hello")
    assert "Kay likes tea" in out
    # seeded flag: later turns don't re-queue from on_turn_start
    p.on_turn_start(2, "more")
    assert p.prefetch("more") == ""
