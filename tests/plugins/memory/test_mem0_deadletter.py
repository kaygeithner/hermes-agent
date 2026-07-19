"""Tests for the mem0 dead-letter queue and current-turn prefetch."""

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
    # join the sync worker first — it may start the replay thread on success,
    # so _replay_thread must be re-read AFTER the sync join
    if p._sync_thread:
        p._sync_thread.join(timeout=10)
    if p._replay_thread:
        p._replay_thread.join(timeout=10)


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
    monkeypatch.setattr(mem0_mod, "_DEADLETTER_TRIM_BYTES", 5000)
    p = _provider(tmp_path, monkeypatch, FakeBackend())
    for i in range(_DEADLETTER_MAX + 20):
        p._deadletter_append(f"u{i}", "a")
    lines = _deadletter(tmp_path).read_text().splitlines()
    # bounded by count AND bytes; oldest dropped, newest kept
    assert 0 < len(lines) <= _DEADLETTER_MAX
    assert _deadletter(tmp_path).stat().st_size <= 5000 + 300  # one-entry slack
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
    # ts=10/20 are ancient, so replays carry the stale-date annotation —
    # compare the trailing original content
    contents = [m[0]["content"].rsplit("\n", 1)[-1] for m in backend.added]
    assert contents == ["live", "older", "newer"]


def test_stale_replay_is_date_annotated_fresh_is_not(tmp_path, monkeypatch):
    backend = FakeBackend(fail=True)
    p = _provider(tmp_path, monkeypatch, backend)
    _sync_and_join(p, "fresh fact", "a")  # queued with a just-now ts
    path = _deadletter(tmp_path)
    with path.open("a") as f:  # plus one ancient entry
        f.write(json.dumps({"ts": 1, "messages": [{"role": "user", "content": "old fact"}, {"role": "assistant", "content": "a"}]}) + "\n")
    backend.fail = False
    _sync_and_join(p, "live", "a")
    by_tail = {m[0]["content"].rsplit("\n", 1)[-1]: m[0]["content"] for m in backend.added}
    assert "restored from an offline queue" in by_tail["old fact"]
    assert by_tail["fresh fact"] == "fresh fact"  # byte-identical, no note


def test_shutdown_flag_stops_drain_without_attempts_bump(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("queued", "x")
    p._shutting_down = True
    # no new drain is spawned during shutdown...
    p._start_deadletter_replay(backend)
    assert p._replay_thread is None
    # ...and a running drain exits between entries without touching attempts
    p._deadletter_replay(backend)
    assert backend.added == []
    entry = json.loads(_deadletter(tmp_path).read_text().splitlines()[0])
    assert "attempts" not in entry


def test_mutate_failure_survives_and_does_not_duplicate(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("queued", "x")

    def boom(remove=(), replace=None):
        raise OSError("disk full")

    # plain instance-attribute shadow — NOT monkeypatch.setattr, whose undo()
    # would also revert the HERMES_HOME redirect and touch the real queue
    p._deadletter_mutate = boom
    _sync_and_join(p, "live1", "a")  # add succeeded, removal failed
    # replay errored but the workers survived; next sync still works and the
    # already-ingested entry is removed, NOT re-added
    del p._deadletter_mutate
    _sync_and_join(p, "live2", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents[0] == "live1"
    assert "live2" in contents
    assert contents.count("queued") == 1
    assert _drained(tmp_path)


def test_transient_404_flap_is_not_dropped(tmp_path, monkeypatch):
    class FlakyBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.flap = True

        def add(self, messages, **kwargs):
            if messages[0]["content"] == "queued" and self.flap:
                self.flap = False
                raise ConnectionError(
                    "Unexpected Response: 404 (Not Found) — collection recovering")
            return super().add(messages, **kwargs)

    backend = FlakyBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("queued", "x")
    _sync_and_join(p, "live1", "a")  # replay hits the 404-shaped flap
    # entry must still be queued (attempts=1), not dropped on sight
    lines = _deadletter(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["attempts"] == 1
    _sync_and_join(p, "live2", "a")  # flap over — replays fine
    contents = [m[0]["content"] for m in backend.added]
    assert "queued" in contents
    assert _drained(tmp_path)


def test_attempts_bump_preserves_file_position(tmp_path, monkeypatch):
    class HeadFails(FakeBackend):
        def add(self, messages, **kwargs):
            if messages[0]["content"].endswith("older"):  # stale replays carry a date note
                raise ConnectionError("flaky")
            return super().add(messages, **kwargs)

    p = _provider(tmp_path, monkeypatch, HeadFails())
    path = _deadletter(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(json.dumps({"ts": 10, "messages": [{"role": "user", "content": "older"}, {"role": "assistant", "content": "a"}]}) + "\n")
        f.write(json.dumps({"ts": 20, "messages": [{"role": "user", "content": "newer"}, {"role": "assistant", "content": "a"}]}) + "\n")
    _sync_and_join(p, "live", "a")
    lines = path.read_text().splitlines()
    # the failing head entry is updated in place, not re-appended at the tail
    assert json.loads(lines[0])["messages"][0]["content"] == "older"
    assert json.loads(lines[0])["attempts"] == 1
    assert json.loads(lines[1])["messages"][0]["content"] == "newer"


def test_unclassified_permanent_error_dropped_after_max_attempts(tmp_path, monkeypatch):
    from plugins.memory.mem0 import _DEADLETTER_MAX_ATTEMPTS

    class PayloadTooLarge(Exception):  # not matched by _is_client_error
        pass

    class PoisonBackend(FakeBackend):
        def add(self, messages, **kwargs):
            if messages[0]["content"] == "poison":
                raise PayloadTooLarge("413 request entity too large")
            return super().add(messages, **kwargs)

    backend = PoisonBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    p._deadletter_append("poison", "x")
    p._deadletter_append("good", "y")
    for i in range(_DEADLETTER_MAX_ATTEMPTS):
        _sync_and_join(p, f"live{i}", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert "good" in contents  # drained once the poison entry hit the cap
    assert "poison" not in contents
    assert _drained(tmp_path)


def test_append_after_truncated_tail_does_not_merge(tmp_path, monkeypatch):
    backend = FakeBackend()
    p = _provider(tmp_path, monkeypatch, backend)
    path = _deadletter(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # crash mid-append: partial line, no trailing newline
    path.write_bytes(b'{"ts": 1, "messages": [{"role": "user", "content": "partial')
    p._deadletter_append("good", "turn")
    _sync_and_join(p, "live", "a")
    contents = [m[0]["content"] for m in backend.added]
    assert contents == ["live", "good"]


def test_append_failure_reports_not_queued(tmp_path, monkeypatch):
    backend = FakeBackend(fail=True)
    p = _provider(tmp_path, monkeypatch, backend)
    monkeypatch.setattr(p, "_deadletter_path", lambda: (_ for _ in ()).throw(OSError("no home")))
    assert p._deadletter_append("u", "a") is False


def test_current_turn_prefetch(tmp_path, monkeypatch):
    backend = FakeBackend()
    backend.search_results = [{"memory": "Kay likes tea"}]
    p = _provider(tmp_path, monkeypatch, backend)
    p.on_turn_start(1, "hello")
    assert "Kay likes tea" in p.prefetch("hello")

    backend.search_results = [{"memory": "Kay likes coffee"}]
    p.on_turn_start(2, "more")
    assert "Kay likes coffee" in p.prefetch("more")
