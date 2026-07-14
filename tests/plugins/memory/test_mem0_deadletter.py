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


def _drained(tmp_path):
    path = _deadletter(tmp_path)
    return not path.exists() or path.read_text().strip() == ""


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
    assert _drained(tmp_path)


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
    import plugins.memory.mem0 as mem0_mod
    monkeypatch.setattr(mem0_mod, "_DEADLETTER_TRIM_BYTES", 1)  # trim on every append
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
    assert _drained(tmp_path)


def test_unicode_line_separators_survive_roundtrip(tmp_path, monkeypatch):
    # U+2028/U+2029 pass through json.dumps(ensure_ascii=False) unescaped;
    # splitlines() would fragment them — split must be on "\n" only.
    backend = FakeBackend(fail=True)
    p = _provider(tmp_path, monkeypatch, backend)
    tricky = "line one\u2028line two\u2029line three"
    _sync_and_join(p, tricky, "answermore")
    backend.fail = False
    _sync_and_join(p, "live", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", tricky]
    assert _drained(tmp_path)


def test_truncated_utf8_does_not_wedge_replay(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("good", "turn")
    path = _deadletter(tmp_path)
    # simulate a crash mid-append inside a multibyte character
    with path.open("ab") as f:
        f.write(b'{"ts": 1, "messages": [{"role": "user", "content": "caf\xc3')
    _sync_and_join(p, "live", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", "good"]


def test_permanently_rejected_entry_is_dropped(tmp_path, monkeypatch):
    class ValidationError(Exception):
        pass

    class PoisonBackend(FakeBackend):
        def add(self, messages, **kwargs):
            if messages[0]["content"] == "poison":
                raise ValidationError("bad content")
            return super().add(messages, **kwargs)

    backend = PoisonBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("poison", "x")
    p._deadletter_append("good", "y")
    _sync_and_join(p, "live", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", "good"]
    assert _drained(tmp_path)


def test_replay_is_oldest_first_by_ts(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    path = _deadletter(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # file order inverted vs timestamps (busy-skip vs failed-sync interleave)
    with path.open("w") as f:
        f.write(json.dumps({"ts": 20, "messages": [{"role": "user", "content": "newer"}, {"role": "assistant", "content": "a"}]}) + "\n")
        f.write(json.dumps({"ts": 10, "messages": [{"role": "user", "content": "older"}, {"role": "assistant", "content": "a"}]}) + "\n")
    _sync_and_join(p, "live", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", "older", "newer"]


def test_pop_failure_does_not_kill_sync_worker(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("queued", "x")
    monkeypatch.setattr(p, "_deadletter_pop", lambda line: (_ for _ in ()).throw(OSError("disk full")))
    _sync_and_join(p, "live1", "a")
    # replay errored but the worker survived; next sync still works
    monkeypatch.undo()
    _sync_and_join(p, "live2", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents[0] == "live1"
    assert "live2" in contents


def test_append_failure_reports_not_queued(tmp_path, monkeypatch):
    backend = FakeBackend(fail=True)
    p = _provider(tmp_path, monkeypatch, backend)
    monkeypatch.setattr(p, "_deadletter_path", lambda: (_ for _ in ()).throw(OSError("no home")))
    assert p._deadletter_append("u", "a") is False


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
