"""Tests for the mem0 dead-letter queue and current-turn prefetch."""

import json
import multiprocessing
import os
import threading
import time

import pytest

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
    path = _deadletter(tmp_path)
    lines = path.read_text().splitlines()
    assert path.stat().st_mode & 0o777 == 0o600
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
    assert _deadletter(tmp_path).stat().st_mode & 0o777 == 0o600
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


def test_reinitialize_cannot_let_old_backend_drain_shared_queue(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingBackend(FakeBackend):
        def add(self, messages, **kwargs):
            self.added.append(messages)
            started.set()
            assert release.wait(timeout=5)
            return {}

    old_backend = BlockingBackend()
    p = _provider(tmp_path, monkeypatch, old_backend)
    p._atexit_registered = True
    p._deadletter_append("turn-A", "answer-A", ts=1)
    p._deadletter_append("turn-B", "answer-B", ts=2)

    old_thread = threading.Thread(
        target=p._deadletter_replay, args=(old_backend,), daemon=True
    )
    old_thread.start()
    assert started.wait(timeout=5)

    new_backend = FakeBackend()
    monkeypatch.setattr(
        "plugins.memory.mem0._load_config",
        lambda: {"mode": "oss", "oss": {}, "user_id": "kay"},
    )
    monkeypatch.setattr(p, "_create_backend", lambda: new_backend)
    p.initialize("new-session", hermes_home=str(tmp_path))
    release.set()
    old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert len(old_backend.added) == 1
    assert old_backend.added[0][0]["content"].endswith("turn-A")
    assert new_backend.added == []
    queued = [
        json.loads(line)["messages"][0]["content"]
        for line in _deadletter(tmp_path).read_text().splitlines()
    ]
    assert queued == ["turn-A", "turn-B"]

    p._deadletter_replay(new_backend)
    assert [
        m[0]["content"].rsplit("\n", 1)[-1] for m in new_backend.added
    ] == ["turn-A", "turn-B"]
    assert _drained(tmp_path)


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


def test_deadletter_creation_is_atomic_0600_and_fsyncs_parent(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, FakeBackend())
    path = _deadletter(tmp_path)
    real_open = os.open
    opens = []
    fsyncs = []

    def tracked_open(raw, flags, mode=0o777):
        opens.append((os.fspath(raw), flags, mode))
        return real_open(raw, flags, mode)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(fd))

    assert p._deadletter_append("private", "turn") is True
    queue_opens = [c for c in opens if c[0] == os.fspath(path)]
    assert queue_opens
    _, flags, mode = queue_opens[-1]
    assert flags & os.O_CREAT
    assert flags & os.O_APPEND
    assert mode == 0o600
    assert len(fsyncs) >= 2  # queue contents plus containing directory


def test_deadletter_rewrite_uses_secure_temp_and_fsyncs_parent(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, FakeBackend())
    path = _deadletter(tmp_path)
    path.parent.mkdir(parents=True)
    real_open = os.open
    opens = []
    fsyncs = []

    def tracked_open(raw, flags, mode=0o777):
        opens.append((os.fspath(raw), flags, mode))
        return real_open(raw, flags, mode)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(fd))
    p._deadletter_write(path, [json.dumps({"ts": 1, "messages": []})])

    temp = os.fspath(path.with_suffix(".jsonl.tmp"))
    temp_opens = [c for c in opens if c[0] == temp]
    assert temp_opens
    assert temp_opens[-1][2] == 0o600
    assert len(fsyncs) >= 2  # temp contents plus rename directory


def test_windows_drain_lock_is_nonblocking(tmp_path, monkeypatch):
    import plugins.memory.mem0 as mem0_mod

    calls = []

    class FakeMsvcrt:
        LK_NBLCK = 2

        @staticmethod
        def locking(fd, mode, count):
            calls.append((fd, mode, count))

    monkeypatch.setattr(mem0_mod, "fcntl", None)
    monkeypatch.setattr(mem0_mod, "msvcrt", FakeMsvcrt, raising=False)
    lock = Mem0MemoryProvider._try_drain_lock(tmp_path / "drain.lock")
    assert lock is not None
    lock.close()
    assert calls and calls[0][1:] == (FakeMsvcrt.LK_NBLCK, 1)


def test_shutdown_uses_one_deadline_and_prioritizes_sync(monkeypatch):
    p = Mem0MemoryProvider()
    joins = []

    class FakeThread:
        def __init__(self, name):
            self.name = name

        def is_alive(self):
            return True

        def join(self, timeout):
            joins.append((self.name, timeout))

    class SyncThread(FakeThread):
        def join(self, timeout):
            super().join(timeout)
            p._replay_thread = FakeThread("replay")

    p._sync_thread = SyncThread("sync")
    p._replay_thread = None
    p._prefetch_thread = FakeThread("prefetch")
    ticks = iter((100.0, 100.0, 110.0, 125.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(p, "_shutdown_backend", lambda: None)

    p.shutdown()

    assert [name for name, _ in joins] == ["sync", "replay", "prefetch"]
    assert [timeout for _, timeout in joins] == [30.0, 20.0, 5.0]


def test_current_turn_prefetch(tmp_path, monkeypatch):
    backend = FakeBackend()
    backend.search_results = [{"memory": "Kay likes tea"}]
    p = _provider(tmp_path, monkeypatch, backend)
    p.on_turn_start(1, "hello")
    assert "Kay likes tea" in p.prefetch("hello")

    backend.search_results = [{"memory": "Kay likes coffee"}]
    p.on_turn_start(2, "more")
    assert "Kay likes coffee" in p.prefetch("more")


def test_sync_turn_accepts_v0182_message_context(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, FakeBackend())
    p.sync_turn("hello", "world", messages=[{"role": "user", "content": "hello"}])
    assert p._sync_thread is not None
    p._sync_thread.join(timeout=10)
    assert not p._sync_thread.is_alive()


def test_superseded_prefetch_does_not_mutate_breaker_state(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = {"same": 0}

    class SlowFirstBackend(FakeBackend):
        def search(self, query, *, filters, top_k=10, rerank=True):
            if query == "same":
                calls["same"] += 1
                if calls["same"] == 1:
                    started.set()
                    assert release.wait(timeout=5)
                    raise ConnectionError("stale failure")
                return [{"memory": "current result"}]
            return []

    p = _provider(tmp_path, monkeypatch, SlowFirstBackend())
    p._consecutive_failures = 1
    p._start_prefetch("same")
    stale_thread = p._prefetch_thread
    assert stale_thread is not None
    assert started.wait(timeout=5)

    p._start_prefetch("other")
    middle_thread = p._prefetch_thread
    assert middle_thread is not None
    middle_thread.join(timeout=5)
    assert not middle_thread.is_alive()

    p._start_prefetch("same")
    current_thread = p._prefetch_thread
    assert current_thread is not None
    current_thread.join(timeout=5)
    assert not current_thread.is_alive()
    assert p._consecutive_failures == 0

    release.set()
    stale_thread.join(timeout=5)
    assert not stale_thread.is_alive()
    assert p._consecutive_failures == 0
    assert p._prefetch_query == "same"
    assert p._prefetch_result == "## Mem0 Memory\n- current result"
    assert p._prefetch_done is True


def test_reinitialize_discards_late_prefetch_result(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingBackend(FakeBackend):
        def search(self, query, *, filters, top_k=10, rerank=True):
            started.set()
            assert release.wait(timeout=5)
            return [{"memory": "stale result"}]

    p = _provider(tmp_path, monkeypatch, BlockingBackend())
    p._atexit_registered = True
    p._consecutive_failures = 3
    p._start_prefetch("same query")
    old_thread = p._prefetch_thread
    assert old_thread is not None
    assert started.wait(timeout=5)

    monkeypatch.setattr(
        "plugins.memory.mem0._load_config",
        lambda: {"mode": "oss", "oss": {}, "user_id": "kay"},
    )
    monkeypatch.setattr(p, "_create_backend", FakeBackend)
    p.initialize("new-session", hermes_home=str(tmp_path))
    release.set()
    old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert p._consecutive_failures == 3
    assert p._prefetch_result == ""
    assert p._prefetch_done is False


def test_reinitialize_isolates_late_sync_failure_context(tmp_path, monkeypatch):
    """A stale sync must queue to its launch profile without poisoning the new one."""
    started = threading.Event()
    release = threading.Event()

    class BlockingFailBackend(FakeBackend):
        def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
            started.set()
            assert release.wait(timeout=5)
            raise ConnectionError("old backend failed")

    old_home = tmp_path / "old-profile"
    new_home = tmp_path / "new-profile"
    old_home.mkdir()
    new_home.mkdir()

    p = _provider(old_home, monkeypatch, BlockingFailBackend())
    p._hermes_home = str(old_home)
    p._user_id = "old-user"
    p._agent_id = "old-agent"
    p._channel = "old-channel"
    p._atexit_registered = True
    p.sync_turn("old turn", "old answer")
    old_thread = p._sync_thread
    assert old_thread is not None
    assert started.wait(timeout=5)

    replacement = FakeBackend()
    monkeypatch.setattr(
        "plugins.memory.mem0._load_config",
        lambda: {
            "mode": "oss",
            "oss": {},
            "user_id": "new-user",
            "agent_id": "new-agent",
        },
    )
    monkeypatch.setattr(p, "_create_backend", lambda: replacement)
    p.initialize(
        "new-session",
        hermes_home=str(new_home),
        platform="new-channel",
    )

    release.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()
    assert p._backend is replacement
    assert p._consecutive_failures == 0
    assert not _deadletter(new_home).exists()

    lines = _deadletter(old_home).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["messages"][0]["content"] == "old turn"
    assert entry["user_id"] == "old-user"
    assert entry["agent_id"] == "old-agent"
    assert entry["metadata"] == {"channel": "old-channel"}


# These workers deliberately stop on a durability boundary. They stay at module
# scope so multiprocessing can launch them without pickling test closures.
def _park_forever():
    while True:
        time.sleep(1)


def _append_boundary_worker(home, ready):
    os.environ["HERMES_HOME"] = home
    p = Mem0MemoryProvider()
    p._config = {"mode": "oss", "oss": {}}
    p._mode = "oss"
    p._hermes_home = home
    real_fsync_parent = p._fsync_parent

    def pause_after_content_fsync(path):
        if os.fspath(path).endswith("mem0-deadletter.jsonl"):
            ready.set()
            _park_forever()
        real_fsync_parent(path)

    p._fsync_parent = pause_after_content_fsync
    try:
        p._deadletter_append("append boundary", "answer")
    finally:
        p._fsync_parent = real_fsync_parent


def _rewrite_boundary_worker(home, ready):
    os.environ["HERMES_HOME"] = home
    from plugins.memory import mem0 as mem0_mod

    p = Mem0MemoryProvider()
    p._hermes_home = home
    path = p._deadletter_path()
    real_replace = mem0_mod.atomic_replace

    def pause_after_replace(source, destination):
        real_replace(source, destination)
        ready.set()
        _park_forever()

    mem0_mod.atomic_replace = pause_after_replace
    p._deadletter_write(
        path,
        [json.dumps({"ts": 2, "messages": [{"role": "user", "content": "new"}]})],
    )


def _replay_boundary_worker(home, ready, receipt):
    os.environ["HERMES_HOME"] = home

    class BlockingBackend(FakeBackend):
        def add(self, messages, **kwargs):
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(messages[0]["content"])
                stream.flush()
                os.fsync(stream.fileno())
            ready.set()
            _park_forever()
            return {}

    p = Mem0MemoryProvider()
    p._config = {"mode": "oss", "oss": {}}
    p._mode = "oss"
    p._hermes_home = home
    backend = BlockingBackend()
    setattr(p, "_backend", backend)
    p._deadletter_replay(backend)


@pytest.mark.skipif(os.name != "posix", reason="forced-termination durability gate is POSIX-only")
def test_forced_termination_preserves_append_and_rewrite_boundaries(tmp_path):
    ctx = multiprocessing.get_context("fork")
    home = os.fspath(tmp_path)
    path = _deadletter(tmp_path)

    # Append has fsynced the complete JSONL record before the child is killed.
    ready = ctx.Event()
    proc = ctx.Process(target=_append_boundary_worker, args=(home, ready))
    proc.start()
    assert ready.wait(timeout=10)
    proc.kill()
    proc.join(timeout=10)
    assert not proc.is_alive()
    lines = path.read_text(encoding="utf-8").split("\n")
    entries = [json.loads(line) for line in lines if line]
    assert entries[0]["messages"][0]["content"] == "append boundary"
    assert path.stat().st_mode & 0o777 == 0o600

    # Rewrite has atomically replaced the queue before the child is killed.
    ready = ctx.Event()
    proc = ctx.Process(target=_rewrite_boundary_worker, args=(home, ready))
    proc.start()
    assert ready.wait(timeout=10)
    proc.kill()
    proc.join(timeout=10)
    assert not proc.is_alive()
    rewritten = [json.loads(line) for line in path.read_text().split("\n") if line]
    assert rewritten[0]["messages"][0]["content"] == "new"
    assert not path.with_suffix(".jsonl.tmp").exists()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="forced-termination durability gate is POSIX-only")
def test_forced_termination_during_replay_keeps_queue_recoverable(tmp_path, monkeypatch):
    ctx = multiprocessing.get_context("fork")
    home = os.fspath(tmp_path)
    receipt = os.fspath(tmp_path / "backend-receipt")
    p = _provider(tmp_path, monkeypatch, FakeBackend())
    assert p._deadletter_append("replay boundary", "answer")

    ready = ctx.Event()
    proc = ctx.Process(target=_replay_boundary_worker, args=(home, ready, receipt))
    proc.start()
    assert ready.wait(timeout=10)
    proc.kill()
    proc.join(timeout=10)
    assert not proc.is_alive()

    # Backend side effect may have happened, so replay is intentionally
    # at-least-once. The local queue must remain valid and drain on retry.
    assert os.path.exists(receipt)
    json.loads(_deadletter(tmp_path).read_text().split("\n", 1)[0])
    backend = FakeBackend()
    setattr(p, "_backend", backend)
    p._deadletter_replay(backend)
    assert backend.added[0][0]["content"] == "replay boundary"
    assert _drained(tmp_path)
