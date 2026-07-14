"""Mem0 memory plugin — MemoryProvider interface.

Server-side LLM fact extraction, semantic search, and automatic deduplication
via the Mem0 Platform API (cloud) or OSS (self-hosted) via Memory.

Original PR #2933 by kartik-mem0, adapted to MemoryProvider ABC.

Configuration
-------------
Secret (lives in $HERMES_HOME/.env or the environment):
  MEM0_API_KEY       — Mem0 Platform API key (required for platform mode)
  MEM0_HOST          — Base URL of a self-hosted Mem0 server. When set, the
                       plugin talks to that server directly over HTTP
                       (X-API-Key auth) instead of the cloud API.

Behavioral settings (live in $HERMES_HOME/mem0.json, set via `hermes memory
setup`):
  mode               — Backend mode: "platform" (default) or "oss"
  host               — Self-hosted Mem0 server URL (alt: MEM0_HOST env var).
                       When set, routes to the self-hosted HTTP backend.
  user_id            — Canonical user identifier. When set, it is applied
                       uniformly across every gateway (CLI, Telegram, Slack,
                       Discord, …) so the same human gets one merged memory
                       store. When unset, the gateway-native id (e.g. Telegram
                       numeric id, Discord snowflake) is used instead.
  agent_id           — Agent identifier (default: hermes)

The matching MEM0_MODE / MEM0_USER_ID / MEM0_AGENT_ID environment variables are
still read as a backward-compatible fallback, but mem0.json is the canonical
home for these non-secret settings.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

try:
    import fcntl
except ImportError:  # Windows — no cross-process drain exclusion (dedup absorbs)
    fcntl = None

from agent.memory_provider import MemoryProvider
from tools.memory_tool import MemoryStore
from tools.registry import tool_error
from utils import atomic_replace

logger = logging.getLogger(__name__)

# Circuit breaker: after this many consecutive failures, pause API calls
# for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120
_PREFETCH_WAIT_SECS = 3

_CLIENT_ERROR_TYPES = ("MemoryNotFoundError", "ValidationError")

# Dead-letter queue: turns that failed to sync (backend down, breaker open,
# previous sync still in flight) are appended here and replayed after the
# next successful sync. Bounded — oldest entries beyond this are dropped.
_DEADLETTER_MAX = 200
# Appends are cheap (no read-back); the trim runs only once the file grows
# past this many bytes, and re-bounds the file by count AND bytes.
_DEADLETTER_TRIM_BYTES = 2_000_000
# An entry that keeps failing replay while the backend is otherwise healthy
# (the current sync succeeded) is presumed poisoned and dropped after this
# many attempts — catches permanent rejections _is_client_error can't name.
_DEADLETTER_MAX_ATTEMPTS = 8
# Replayed entries older than this get a date annotation prepended to the
# user message so mem0's extraction doesn't regress newer facts to stale
# ones (an outage drain replays old turns AFTER newer live syncs).
_DEADLETTER_ANNOTATE_AGE_SECS = 600

# Sentinel returned when neither MEM0_USER_ID nor a gateway-native id is
# available. Treated as "no operator-configured user_id" by initialize() so
# that legacy mem0.json files written by the setup wizard (which historically
# wrote this exact placeholder) still allow gateway-native ids to flow
# through instead of silently overriding them with the placeholder.
_DEFAULT_USER_ID = "hermes-user"


def _is_client_error(exc: Exception) -> bool:
    """True for user-caused errors (bad ID, not found) that should NOT trip circuit breaker."""
    etype = type(exc).__name__
    if etype in _CLIENT_ERROR_TYPES:
        return True
    err_str = str(exc).lower()
    return "404" in err_str or "not found" in err_str or "valid uuid" in err_str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from env vars, with $HERMES_HOME/mem0.json overrides.

    Environment variables provide defaults; mem0.json (if present) overrides
    individual keys.  This avoids a silent failure when the JSON file exists
    but is missing fields like ``api_key`` that the user set in ``.env``.
    """
    from hermes_constants import get_hermes_home

    config = {
        "mode": os.environ.get("MEM0_MODE", "platform"),
        "api_key": os.environ.get("MEM0_API_KEY", ""),
        "host": os.environ.get("MEM0_HOST", ""),
        "agent_id": os.environ.get("MEM0_AGENT_ID", "hermes"),
        "oss": {},
    }
    # Only carry user_id when the operator explicitly configured one (env or
    # mem0.json). An absent key tells initialize() to fall back to the
    # gateway-native id from kwargs instead of overriding it with a placeholder.
    env_user_id = os.environ.get("MEM0_USER_ID")
    if env_user_id:
        config["user_id"] = env_user_id

    config_path = get_hermes_home() / "mem0.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    return config


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": (
        "Search the user's memories by meaning; returns facts ranked by "
        "relevance. Use this before answering any question that may depend on "
        "what you know about the user (preferences, facts, history, people, "
        "projects, past decisions). For multi-part or multi-hop questions, "
        "call it several times — vary the wording and run follow-up searches "
        "on what earlier results reveal; one search is rarely enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
            "rerank": {"type": "boolean", "description": "Rerank results for relevance (default: false, platform mode only)."},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "mem0_add",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM extraction). "
        "Call this the moment the user states a lasting preference, correction, "
        "decision, or personal detail worth recalling on future turns — don't "
        "wait to be asked to remember. Skip transient chit-chat and facts you've "
        "already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
        },
        "required": ["content"],
    },
}

UPDATE_SCHEMA = {
    "name": "mem0_update",
    "description": (
        "Replace the text of an existing memory by its ID (take the ID from a "
        "mem0_search result). Use when a stored fact has changed "
        "or was wrong — correct it in place instead of adding a duplicate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to update."},
            "text": {"type": "string", "description": "New text content."},
        },
        "required": ["memory_id", "text"],
    },
}

DELETE_SCHEMA = {
    "name": "mem0_delete",
    "description": (
        "Delete a memory by its ID (take the ID from a mem0_search "
        "result). Use when a stored fact is obsolete or the user asks you to "
        "forget it; prefer mem0_update if the fact merely changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to delete."},
        },
        "required": ["memory_id"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class Mem0MemoryProvider(MemoryProvider):
    """Mem0 memory with server-side extraction and semantic search.

    Supports Platform API (cloud) and OSS (self-hosted) modes via MEM0_MODE.
    """

    def __init__(self):
        self._config = None
        self._backend = None
        self._mode = "platform"
        self._api_key = ""
        self._host = ""
        self._user_id = _DEFAULT_USER_ID
        self._agent_id = "hermes"
        self._rerank_default = False
        self._channel = "cli"  # gateway channel name (cli/telegram/discord/...)
        self._sync_thread = None
        self._prefetch_thread = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        self._replay_thread = None
        self._shutting_down = False
        # Entries backend.add already ingested whose file removal failed
        # (e.g. ENOSPC) — skipped on later passes so they aren't re-added.
        self._replayed_pending = set()
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._deadletter_lock = threading.Lock()
        self._prefetch_seeded = False
        self._atexit_registered = False

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        cfg = _load_config()
        mode = cfg.get("mode", "platform")
        if mode == "oss":
            return bool(cfg.get("oss", {}).get("vector_store"))
        # Platform needs an api_key; self-hosted needs a host (api_key optional
        # when the server runs with AUTH_DISABLED).
        return bool(cfg.get("api_key") or cfg.get("host"))

    def save_config(self, values, hermes_home):
        """Write config to $HERMES_HOME/mem0.json."""
        import json
        from pathlib import Path
        config_path = Path(hermes_home) / "mem0.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        from utils import atomic_json_write
        atomic_json_write(config_path, existing, mode=0o600)

    def get_config_schema(self):
        cfg = _load_config()
        mode = cfg.get("mode", "platform")
        api_key_required = mode != "oss"
        return [
            {"key": "api_key", "description": "Mem0 Platform API key", "secret": True, "required": api_key_required, "env_var": "MEM0_API_KEY", "url": "https://app.mem0.ai"},
            {"key": "host", "description": "Self-hosted Mem0 server URL (leave blank for cloud)", "required": False, "env_var": "MEM0_HOST"},
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
            {"key": "rerank", "description": "Enable reranking for recall", "default": "false", "choices": ["true", "false"]},
        ]

    def post_setup(self, hermes_home: str, config: dict) -> None:
        from ._setup import post_setup
        post_setup(hermes_home, config)

    def _create_backend(self):
        # Lazy-install the mem0 SDK on demand before either backend imports
        # it. ensure() honors security.allow_lazy_installs (default true) and,
        # on a sealed Docker venv, redirects the install to the durable
        # target. On failure we fall through so the import inside the backend
        # produces the canonical error, captured below.
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("memory.mem0", prompt=False)
        except ImportError:
            pass
        except Exception:
            pass
        try:
            if self._mode == "oss":
                from ._backend import OSSBackend
                return OSSBackend(self._config.get("oss", {}))
            if self._host:
                from ._backend import SelfHostedBackend
                return SelfHostedBackend(self._api_key, self._host)
            from ._backend import PlatformBackend
            return PlatformBackend(self._api_key)
        except Exception as e:
            logger.error("Mem0 backend failed to initialize (%s mode): %s", self._mode, e)
            self._init_error = str(e)
            return None

    def _is_breaker_open(self) -> bool:
        """Return True if the circuit breaker is tripped (too many failures)."""
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                return False
            return True

    def _format_error(self, prefix: str, exc: Exception) -> str:
        msg = f"{prefix}: {exc}"
        if self._mode == "oss":
            err_str = str(exc).lower()
            if "connection" in err_str or "refused" in err_str or "timeout" in err_str:
                vs = self._config.get("oss", {}).get("vector_store", {})
                msg += f" (check that {vs.get('provider', 'vector store')} is running)"
        return msg

    def _record_success(self):
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._breaker_lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            else:
                count = 0
        if count >= _BREAKER_THRESHOLD:
            hint = ""
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                provider = vs.get("provider", "unknown")
                hint = f" Check that your {provider} vector store is running and reachable."
            logger.warning(
                "Mem0 circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.%s",
                count, _BREAKER_COOLDOWN_SECS, hint,
            )

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        self._mode = self._config.get("mode", "platform")
        self._api_key = self._config.get("api_key", "")
        self._host = self._config.get("host", "")
        # Resolution order for user_id:
        #   1. Operator-configured MEM0_USER_ID (env or $HERMES_HOME/mem0.json) —
        #      the canonical principal, applied across every gateway so the same
        #      human gets one merged memory store.
        #   2. Gateway-native id from kwargs (Telegram numeric id, Discord
        #      snowflake, etc.) — preserves per-platform isolation when no
        #      override is configured.
        #   3. Hardcoded fallback _DEFAULT_USER_ID (CLI with no auth).
        # The literal _DEFAULT_USER_ID string is treated as unset so users who
        # ran the setup wizard with the suggested default still get gateway-
        # native ids instead of being silently bucketed together.
        configured = self._config.get("user_id")
        if configured == _DEFAULT_USER_ID:
            configured = None
        self._user_id = configured or kwargs.get("user_id") or _DEFAULT_USER_ID
        self._agent_id = self._config.get("agent_id", "hermes")
        # Persisted rerank preference (setup wizard / mem0.json). Used as the
        # DEFAULT for mem0_search when the model doesn't pass ``rerank``
        # explicitly; per-call args still win. Platform-only feature — other
        # backends accept-and-ignore the flag.
        _rr = self._config.get("rerank", False)
        self._rerank_default = (
            _rr.lower() in ("true", "1", "yes") if isinstance(_rr, str) else bool(_rr)
        )
        self._channel = kwargs.get("platform") or "cli"
        self._shutting_down = False  # instance may be re-initialized after teardown
        self._backend = self._create_backend()
        if self._backend and not self._atexit_registered:
            atexit.register(self._shutdown_backend)
            self._atexit_registered = True

    def _read_filters(self) -> Dict[str, Any]:
        # Scoped to user_id only — by design — so recall surfaces memories
        # written from any gateway/agent under this principal. Writes attach
        # agent_id (and metadata.channel) so per-agent / per-channel views are
        # still possible at query time when needed; reads default to the wider
        # cross-agent recall.
        return {"user_id": self._user_id}

    def _write_metadata(self) -> Dict[str, Any]:
        # Tag every write with the gateway channel so the dashboard can offer
        # per-channel filtered views without coupling identity to the channel.
        return {"channel": self._channel} if self._channel else {}

    def system_prompt_block(self) -> str:
        # Mirror the precedence in _create_backend (oss > host > platform) so
        # the label always names the backend that actually runs. Checking
        # ``host`` first here would mislabel an ``oss``+``host`` config as
        # self-hosted HTTP even though OSS wins the routing.
        if self._mode == "oss":
            mode_label = "OSS (self-hosted)"
        elif self._host:
            mode_label = "self-hosted (HTTP API)"
        else:
            mode_label = "platform (cloud API)"
        # Rerank is a Mem0 Platform feature only.
        rerank_note = " Rerank is available on search." if (self._mode == "platform" and not self._host) else ""
        return (
            "# Mem0 Memory\n"
            f"Active. Mode: {mode_label}. User: {self._user_id}.\n"
            "You have persistent memory of this user from past conversations. "
            "You should call mem0_search before answering anything that could depend "
            "on prior context (the user's preferences, facts, history, people, "
            "projects, or earlier decisions) — do not rely on the chat window "
            "alone, and do not assume you have no memory.\n"
            "For multi-part or multi-hop questions, run several searches with "
            "different wording/angles and follow-up searches on what the first "
            "results surface; one search is rarely enough. Keep searching until "
            "you have every fact the question needs before you answer.\n"
            "Tools: mem0_search to find memories, mem0_add to store facts, "
            f"mem0_update and mem0_delete to manage by ID.{rerank_note}"
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def _consume_prefetch_result(self, query: str) -> str | None:
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result = self._prefetch_result
            self._prefetch_result = ""
            self._prefetch_done = False
            return result

    def _start_prefetch(self, query: str) -> None:
        if not query or self._backend is None or self._is_breaker_open():
            return
        backend = self._backend
        with self._prefetch_lock:
            if self._prefetch_query == query:
                if self._prefetch_done:
                    return
                if self._prefetch_thread and self._prefetch_thread.is_alive():
                    return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False

        def _run():
            body = ""
            try:
                results = backend.search(
                    query, filters=self._read_filters(), top_k=10, rerank=False,
                )
                lines = [r.get("memory", "") for r in (results or []) if r.get("memory")]
                if lines:
                    body = "## Mem0 Memory\n" + "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 prefetch failed: %s", e)
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_done = True

        t = threading.Thread(target=_run, daemon=True, name="mem0-prefetch")
        with self._prefetch_lock:
            self._prefetch_thread = t
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        # Slow backend: skip injection; mem0_search tool remains the backstop.
        return ""

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        # First turn this provider instance sees (fresh session, or resumed
        # session in a fresh process): queue_prefetch normally runs at the END
        # of each turn, so there is nothing queued yet and prefetch() would
        # return empty. This hook fires before prefetch(), which joins the
        # worker ≤3s — enough for an embed+search round trip — so turn 1 gets
        # recall injected instead of relying on the model calling mem0_search.
        if self._prefetch_seeded or not message:
            return
        self._prefetch_seeded = True
        self.queue_prefetch(message)

    # -- Dead-letter queue ----------------------------------------------------

    def _deadletter_path(self):
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "state" / "mem0-deadletter.jsonl"

    # Splitting is on "\n" only, never str.splitlines(): json.dumps with
    # ensure_ascii=False leaves U+2028/U+2029/U+0085 unescaped inside entries,
    # and splitlines() would fragment those entries into "corrupt" pieces.
    @staticmethod
    def _split_entries(raw: str) -> list:
        return [l for l in raw.split("\n") if l.strip()]

    @staticmethod
    def _deadletter_write(path, lines: list) -> None:
        """Atomically replace the queue file (caller holds the locks).

        fsync before the rename — without it a power loss shortly after the
        rename can leave an empty/zero-filled file on writeback filesystems,
        losing the whole queue in one shot.
        """
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(("\n".join(lines) + "\n") if lines else "")
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)

    @staticmethod
    def _deadletter_bound(lines: list) -> list:
        """Bound the queue by entry count, then by bytes (keep newest)."""
        lines = lines[-_DEADLETTER_MAX:]
        total = sum(len(l.encode("utf-8")) + 1 for l in lines)
        while len(lines) > 1 and total > _DEADLETTER_TRIM_BYTES:
            total -= len(lines.pop(0).encode("utf-8")) + 1
        return lines

    def _deadletter_append(self, user_content: str, assistant_content: str) -> bool:
        """Queue a turn whose sync was dropped; True if durably queued."""
        queued = False
        try:
            entry = json.dumps({
                "ts": time.time(),
                "messages": [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ],
                "user_id": self._user_id,
                "agent_id": self._agent_id,
                "metadata": self._write_metadata(),
            }, ensure_ascii=False)
            with self._deadletter_lock:
                path = self._deadletter_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                with MemoryStore._file_lock(path):
                    # Heal a crash-truncated tail (no trailing newline) so the
                    # new entry doesn't merge into the partial line.
                    needs_nl = False
                    try:
                        with path.open("rb") as f:
                            f.seek(-1, os.SEEK_END)
                            needs_nl = f.read(1) != b"\n"
                    except (OSError, ValueError):
                        pass  # missing or empty file
                    with path.open("a", encoding="utf-8") as f:
                        if needs_nl:
                            f.write("\n")
                        f.write(entry + "\n")
                        f.flush()
                        # fsync: "queued for replay" must survive a power loss
                        # — same durability the rewrite path already provides
                        os.fsync(f.fileno())
                    queued = True
                    try:
                        if path.stat().st_size > _DEADLETTER_TRIM_BYTES:
                            raw = path.read_text(encoding="utf-8", errors="replace")
                            self._deadletter_write(
                                path, self._deadletter_bound(self._split_entries(raw)))
                    except Exception as e:
                        logger.debug(
                            "Mem0 dead-letter trim failed (turn IS queued): %s", e)
        except Exception as e:
            logger.warning(
                "Mem0 dead-letter append failed — turn NOT queued: %s", e,
            )
        return queued

    def _deadletter_mutate(self, remove: list = (), replace: dict = None) -> None:
        """Remove entries and/or replace them IN PLACE, in one atomic rewrite.

        Replacement preserves file position: the byte-cap trim evicts from the
        head, so re-appending an updated entry at the tail would shield a
        failing entry from eviction while sacrificing newer healthy turns.
        """
        with self._deadletter_lock:
            path = self._deadletter_path()
            with MemoryStore._file_lock(path):
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    return  # nothing to mutate; other read/write errors raise
                lines = self._split_entries(raw)
                for old, new in (replace or {}).items():
                    if old in lines:
                        lines[lines.index(old)] = new
                for line in remove:
                    if line in lines:
                        lines.remove(line)
                self._deadletter_write(path, lines)

    def _start_deadletter_replay(self, backend) -> None:
        """Kick off a background drain of the queue after a successful sync.

        Runs on its own thread so a long drain (up to 200 entries × ~11s OSS
        adds) never extends the mem0-sync worker's lifetime — sync_turn's 5s
        join, and the prefetch queued behind it, stay unaffected. Only the
        serialized _sync worker calls this, so the check-then-start needs no
        lock. Concurrent backend use is already the norm here (prefetch
        searches while syncs add).
        """
        # No drain during shutdown: the backend is about to close, and a
        # failure against a closed backend must not count as a replay
        # attempt. Short-lived (oneshot/cron) processes therefore never
        # drain — the long-lived gateway sharing the same HERMES_HOME does.
        if self._shutting_down:
            return
        try:
            path = self._deadletter_path()
            # unlocked fast path: healthy installs that never queued a turn
            # skip the mkdir/flock machinery entirely (a concurrent first
            # append is simply picked up after the next successful sync)
            if not path.exists() or path.stat().st_size == 0:
                return
        except OSError:
            return
        if self._replay_thread and self._replay_thread.is_alive():
            return

        def _drain():
            try:
                # Cross-process drain lock, NON-blocking: two processes sharing
                # HERMES_HOME (gateway + cron oneshot) must not replay the same
                # snapshot twice — and the loser must not park in an
                # uninterruptible flock that stalls its shutdown join; the
                # holder is draining the queue anyway.
                lock = self._try_drain_lock(path.with_suffix(".drain.lock"))
                if lock is None:
                    return
                try:
                    self._deadletter_replay(backend)
                finally:
                    lock.close()
            except Exception as e:
                logger.warning("Mem0 dead-letter replay error: %s", e)

        self._replay_thread = threading.Thread(
            target=_drain, daemon=True, name="mem0-replay")
        self._replay_thread.start()

    @staticmethod
    def _try_drain_lock(lock_path):
        """Acquire the cross-process drain lock without blocking.

        Returns an open file handle holding the flock (close to release),
        or None if another process's drain holds it. Windows has no fcntl;
        it degrades to no cross-process exclusion (dedup absorbs overlap).
        """
        f = lock_path.open("a")
        if fcntl is None:
            return f
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            f.close()
            return None

    def _deadletter_replay(self, backend) -> None:
        """Drain queued turns (oldest-ts first) after a successful sync.

        Pauses on failure; a failing entry can't poison the queue — its
        persisted attempts counter drops it after _DEADLETTER_MAX_ATTEMPTS
        failures (each while the backend was otherwise healthy). No
        drop-on-sight for "client errors": a transient 404-shaped flap
        (qdrant restarting) must not cascade-delete queued turns. Progress
        is durable per entry, so a shutdown mid-drain re-replays at most one
        turn — and mem0's server-side dedup absorbs that.

        Each pass parses the file once and replays sequentially; the outer
        loop re-reads only to pick up entries appended during the drain.
        """
        while True:
            with self._deadletter_lock:
                path = self._deadletter_path()
                try:
                    with MemoryStore._file_lock(path):
                        raw = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return
            entries, corrupt = [], []
            for line in self._split_entries(raw):
                try:
                    e = json.loads(line)
                    messages = e["messages"]
                    ts = e["ts"] if isinstance(e.get("ts"), (int, float)) else 0.0
                    entries.append((ts, line, e, messages))
                except Exception:
                    corrupt.append(line)
            if corrupt:
                self._deadletter_mutate(remove=corrupt)
            if not entries:
                return
            entries.sort(key=lambda t: t[0])
            for ts, line, entry, messages in entries:
                if self._shutting_down:
                    return
                if line not in self._replayed_pending:
                    try:
                        backend.add(
                            self._annotate_stale(messages, ts),
                            user_id=entry.get("user_id") or self._user_id,
                            agent_id=entry.get("agent_id") or self._agent_id,
                            infer=True,
                            metadata=entry.get("metadata") or {},
                        )
                    except Exception as e:
                        if self._shutting_down:
                            return  # closed-backend failure is not an attempt
                        attempts = (entry.get("attempts") or 0) + 1
                        if attempts >= _DEADLETTER_MAX_ATTEMPTS:
                            logger.warning(
                                "Mem0 dead-letter entry failed %d replay attempts — dropping it: %s",
                                attempts, e)
                            self._deadletter_mutate(remove=[line])
                            continue
                        # ponytail: attempts can tick up on the oldest entry
                        # during a flaky-backend window (sync succeeded, replay
                        # add failed) — 8 healthy-sync failures before calling
                        # one turn poisoned is the accepted ceiling
                        entry["attempts"] = attempts
                        self._deadletter_mutate(
                            replace={line: json.dumps(entry, ensure_ascii=False)})
                        logger.warning(
                            "Mem0 dead-letter replay paused (%d turns queued, attempt %d): %s",
                            len(entries), attempts, e,
                        )
                        return
                try:
                    self._deadletter_mutate(remove=[line])
                except Exception as e:
                    # The add went through but the removal rewrite failed
                    # (ENOSPC): remember in-process so later passes don't
                    # re-ingest the same turn once per sync.
                    self._replayed_pending.add(line)
                    logger.warning(
                        "Mem0 dead-letter removal failed — entry marked replayed in-memory: %s", e)
                    return
                self._replayed_pending.discard(line)

    @staticmethod
    def _annotate_stale(messages: list, ts: float) -> list:
        """Prefix stale replays with their original date for the extractor.

        A drain replays old turns AFTER newer live syncs; without a temporal
        hint mem0's LLM update can regress a fresh fact to the stale one.
        Fresh replays (busy-skip, seconds old) pass through byte-identical.
        """
        # ponytail: content annotation because OSS Memory.add rejects the
        # timestamp param (platform-only); switch to timestamp= if OSS mem0
        # ever supports it or the annotation proves too weak
        if not messages:
            return messages
        if ts and (time.time() - ts) <= _DEADLETTER_ANNOTATE_AGE_SECS:
            return messages  # fresh replay — pass through byte-identical
        head = messages[0]
        if not isinstance(head.get("content"), str):
            return messages
        # a missing/mangled ts means unknown age — annotate, don't assume fresh
        stamp = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
                 if ts else "an unknown earlier time")
        note = (f"[Note: exchange restored from an offline queue; it originally "
                f"happened at {stamp} — newer memories may supersede it.]\n")
        return [{**head, "content": note + head["content"]}, *messages[1:]]

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Send the turn to Mem0 for server-side fact extraction (non-blocking)."""
        if self._backend is None or self._is_breaker_open():
            # Backend down or breaker open: queue instead of losing the turn.
            self._deadletter_append(user_content, assistant_content)
            return

        def _sync():
            backend = self._backend
            if backend is None:
                self._deadletter_append(user_content, assistant_content)
                return
            try:
                messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ]
                backend.add(
                    messages,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    infer=True,
                    metadata=self._write_metadata(),
                )
                self._record_success()
            except Exception as e:
                self._record_failure()
                if self._deadletter_append(user_content, assistant_content):
                    logger.warning("Mem0 sync failed (turn queued for replay): %s", e)
                else:
                    logger.warning("Mem0 sync failed and turn could not be queued: %s", e)
                return
            self._start_deadletter_replay(backend)

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=5.0)
            # If still alive after timeout, queue the turn (previously it was
            # skipped outright "to avoid duplicate ingestion" — i.e. dropped);
            # the in-flight sync's replay pass will pick it up.
            if self._sync_thread and self._sync_thread.is_alive():
                self._deadletter_append(user_content, assistant_content)
                return
            self._sync_thread = threading.Thread(target=_sync, daemon=True, name="mem0-sync")
            self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, ADD_SCHEMA, UPDATE_SCHEMA, DELETE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._backend is None:
            err = getattr(self, "_init_error", "unknown error")
            hint = ""
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                provider = vs.get("provider", "vector store")
                hint = f" Check that {provider} is running and reachable."
            return json.dumps({"error": f"Mem0 backend not initialized: {err}.{hint}"})

        if self._is_breaker_open():
            msg = "Mem0 temporarily unavailable (multiple consecutive failures). Will retry automatically."
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                msg += f" Check that your {vs.get('provider', 'vector store')} is running."
            return json.dumps({"error": msg})

        if tool_name == "mem0_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            try:
                top_k = max(1, min(int(args.get("top_k", 10)), 50))
                rerank_raw = args.get("rerank", getattr(self, "_rerank_default", False))
                if isinstance(rerank_raw, str):
                    rerank = rerank_raw.lower() not in ("false", "0", "no")
                else:
                    rerank = bool(rerank_raw)
                results = self._backend.search(query, filters=self._read_filters(), top_k=top_k, rerank=rerank)
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"id": r.get("id"), "memory": r.get("memory", ""),
                          "score": r.get("score", 0)} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                if not _is_client_error(e):
                    self._record_failure()
                return tool_error(self._format_error("Search failed", e))

        elif tool_name == "mem0_add":
            content = args.get("content", "")
            if not content:
                return tool_error("Missing required parameter: content")
            try:
                result = self._backend.add(
                    [{"role": "user", "content": content}],
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    infer=False,
                    metadata=self._write_metadata(),
                )
                self._record_success()
                event_id = result.get("event_id") if isinstance(result, dict) else None
                # Cloud add is async (server-side extraction); OSS and self-hosted store synchronously.
                msg = "Fact stored." if (self._mode == "oss" or self._host) else "Fact queued for storage."
                return json.dumps({"result": msg, "event_id": event_id})
            except Exception as e:
                self._record_failure()
                return tool_error(self._format_error("Failed to store", e))

        elif tool_name == "mem0_update":
            memory_id = args.get("memory_id", "")
            text = args.get("text", "")
            if not memory_id:
                return tool_error("Missing required parameter: memory_id")
            if not text:
                return tool_error("Missing required parameter: text")
            try:
                result = self._backend.update(memory_id, text)
                self._record_success()
                return json.dumps(result)
            except Exception as e:
                if _is_client_error(e):
                    return tool_error(f"Memory not found: {memory_id}")
                self._record_failure()
                return tool_error(self._format_error("Update failed", e))

        elif tool_name == "mem0_delete":
            memory_id = args.get("memory_id", "")
            if not memory_id:
                return tool_error("Missing required parameter: memory_id")
            try:
                result = self._backend.delete(memory_id)
                self._record_success()
                return json.dumps(result)
            except Exception as e:
                if _is_client_error(e):
                    return tool_error(f"Memory not found: {memory_id}")
                self._record_failure()
                return tool_error(self._format_error("Delete failed", e))

        return tool_error(f"Unknown tool: {tool_name}")

    def _shutdown_backend(self):
        # Also reached via atexit (registered in initialize) — raise the
        # shutdown flag here so a drain failing against the closed backend
        # never counts as a poison attempt, whichever teardown path ran.
        self._shutting_down = True
        try:
            if self._backend:
                self._backend.close()
                self._backend = None
        except Exception:
            pass

    def shutdown(self) -> None:
        # 30s, not 5s: an OSS-mode add() runs LLM fact extraction inline and
        # measures ~11s against a remote extraction endpoint. A shorter join
        # abandons the final turn's write in short-lived (oneshot/cron)
        # processes — the exact loss the session-boundary shutdown exists to
        # prevent. Only waits while a sync is actually in flight.
        # Stop the drain between entries and make sure a post-shutdown add
        # failure is never counted as a replay attempt.
        self._shutting_down = True
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=30.0)
        # Re-read AFTER the sync join: a drain the final sync spawned (or one
        # already running) exits at its next between-entries check, so this
        # join is bounded by one in-flight add, not the whole backlog.
        t = self._replay_thread
        if t and t.is_alive():
            t.join(timeout=30.0)
        self._shutdown_backend()


def register(ctx) -> None:
    """Register Mem0 as a memory provider plugin."""
    ctx.register_memory_provider(Mem0MemoryProvider())
