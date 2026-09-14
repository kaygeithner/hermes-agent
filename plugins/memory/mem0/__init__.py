"""Mem0 memory plugin — MemoryProvider interface.

Server-side fact extraction and semantic search via the Mem0 Platform API (cloud), a
self-hosted Mem0 server (MEM0_HOST, HTTP), or OSS Memory. Secrets live in $HERMES_HOME/.env
(MEM0_API_KEY, MEM0_HOST); settings in $HERMES_HOME/mem0.json via `hermes memory setup`:
mode ("platform"|"oss"), host, user_id (canonical id across gateways; unset → gateway-native
id), agent_id. MEM0_* env vars remain a fallback.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret
from tools.memory_tool import MemoryStore
from tools.registry import tool_error
from utils import atomic_replace

logger = logging.getLogger(__name__)

# Circuit breaker: after _BREAKER_THRESHOLD consecutive failures, pause API
# calls for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD, _BREAKER_COOLDOWN_SECS, _PREFETCH_WAIT_SECS = 5, 120, 3
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

# sync_turn sends the whole turn to the backend for fact extraction. OSS embedding
# models often have small context windows (bge-small-zh-v1.5: 512 tokens ≈ 500 chars;
# jina-embeddings-v3: 8192), and oversized turns make backend.add() raise — Ollama
# answers HTTP 500, hosted APIs return INPUT_TOKEN_LIMIT_EXCEEDED — which _try only
# logs, silently dropping the turn's memory extraction. Cap each message up front.
# The default fits a 512-token embedder (measured: 450 OK, 600 -> HTTP 500 on
# bge-small-zh-v1.5:f16); ``sync_max_chars`` in mem0.json raises it for larger windows.
_SYNC_MSG_MAX_CHARS = 450


def _truncate_for_sync(text: str, max_len: int = _SYNC_MSG_MAX_CHARS) -> str:
    """Cap a synced message at its last sentence boundary within ``max_len``.

    Short messages pass through unchanged; long ones keep the last complete
    sentence inside the window so fact extraction still sees coherent statements,
    with a hard cut as fallback when no boundary exists (or one only appears in
    the first third of the window, which usually means unsegmented input).
    """
    if len(text) <= max_len:
        return text
    for sep in ("。", "！", "？", ".\n", ".", "!", "?"):
        cut = text[:max_len].rfind(sep)
        if cut > max_len // 3:
            return text[:cut + 1]
    return text[:max_len]


def _is_client_error(exc: Exception) -> bool:
    """True for user-caused errors (bad ID, not found) that should NOT trip circuit breaker."""
    err_str = str(exc).lower()
    return type(exc).__name__ in _CLIENT_ERROR_TYPES or any(s in err_str for s in ("404", "not found", "valid uuid"))


def _read_mem0_json(config_path: Path) -> dict:
    """Best-effort read of mem0.json; missing/corrupt file -> {}."""
    if config_path.exists():
        with suppress(Exception):
            return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


def _load_config() -> dict:
    """Env vars provide defaults; $HERMES_HOME/mem0.json overrides individual keys.
    Layering avoids a silent failure when the JSON file exists but lacks fields
    like ``api_key`` that the user set in ``.env``."""
    from hermes_constants import get_hermes_home
    config = {"mode": os.environ.get("MEM0_MODE", "platform"), "api_key": get_secret("MEM0_API_KEY", ""), "host": os.environ.get("MEM0_HOST", ""), "agent_id": os.environ.get("MEM0_AGENT_ID", "hermes"), "oss": {}}
    if os.environ.get("MEM0_USER_ID"):  # only when explicitly configured, so initialize() can fall back to the gateway-native id
        config["user_id"] = os.environ["MEM0_USER_ID"]
    file_cfg = _read_mem0_json(get_hermes_home() / "mem0.json")
    config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
    return config


def _schema(name: str, description: str, properties: dict[str, tuple[str, str]], required: list[str]) -> dict:
    props = {k: {"type": t, "description": d} for k, (t, d) in properties.items()}
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": props, "required": required}}


TOOL_SCHEMAS = [
    _schema("mem0_search", "Search the user's memories by meaning; returns facts ranked by relevance. Use this before answering any question that may depend on what you know about the user (preferences, facts, history, people, projects, past decisions). For multi-part or multi-hop questions, call it several times — vary the wording and run follow-up searches on what earlier results reveal; one search is rarely enough.",
            {"query": ("string", "What to search for."), "top_k": ("integer", "Max results (default: 10, max: 50)."), "rerank": ("boolean", "Rerank results for relevance (default: false, platform mode only).")}, ["query"]),
    _schema("mem0_add", "Store a durable fact about the user, verbatim (no LLM extraction). Call this the moment the user states a lasting preference, correction, decision, or personal detail worth recalling on future turns — don't wait to be asked to remember. Skip transient chit-chat and facts you've already stored.",
            {"content": ("string", "The fact to store.")}, ["content"]),
    _schema("mem0_update", "Replace the text of an existing memory by its ID (take the ID from a mem0_search result). Use when a stored fact has changed or was wrong — correct it in place instead of adding a duplicate.",
            {"memory_id": ("string", "Memory UUID to update."), "text": ("string", "New text content.")}, ["memory_id", "text"]),
    _schema("mem0_delete", "Delete a memory by its ID (take the ID from a mem0_search result). Use when a stored fact is obsolete or the user asks you to forget it; prefer mem0_update if the fact merely changed.",
            {"memory_id": ("string", "Memory UUID to delete.")}, ["memory_id"]),
]

_PROMPT_BODY = (
    "You have persistent memory of this user from past conversations. You should call mem0_search before answering anything that could depend on prior context (the user's preferences, facts, history, people, projects, or earlier decisions) — do not rely on the chat window alone, and do not assume you have no memory.\n"
    "For multi-part or multi-hop questions, run several searches with different wording/angles and follow-up searches on what the first results surface; one search is rarely enough. Keep searching until you have every fact the question needs before you answer.\n"
    "Tools: mem0_search to find memories, mem0_add to store facts, mem0_update and mem0_delete to manage by ID."
)


class Mem0MemoryProvider(MemoryProvider):
    """Mem0 memory with server-side extraction and semantic search (platform, self-hosted or OSS)."""

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
        # Session generation for background sync workers. A cached provider may
        # be reinitialized while an old backend call is still in flight.
        self._sync_gen = 0
        self._prefetch_thread = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        # Unique launch token; also invalidates late writes on provider reuse.
        self._prefetch_gen = 0
        self._replay_thread = None
        self._shutting_down = False
        self._replay_started_at = 0.0
        # Entries backend.add already ingested whose file removal failed
        # (e.g. ENOSPC) — skipped on later passes so they aren't re-added.
        self._replayed_pending = set()
        # Resolved on the initialize() thread, where the profile contextvar
        # override is live — background threads must not call
        # get_hermes_home() lazily (they'd fall back to the default home and
        # cross-contaminate profiles).
        self._hermes_home = ""
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._sync_state_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._deadletter_lock = threading.Lock()
        # Default before initialize() resolves the configured cap: bare provider
        # instances (tests, dead-letter workers) must still be able to build
        # turn messages.
        self._sync_max_chars = _SYNC_MSG_MAX_CHARS
        self._atexit_registered = False

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        cfg = _load_config()
        if cfg.get("mode", "platform") == "oss":
            return bool(cfg.get("oss", {}).get("vector_store"))
        return bool(cfg.get("api_key") or cfg.get("host"))  # platform needs a key; self-hosted a host (key optional with AUTH_DISABLED)

    def save_config(self, values, hermes_home):
        """Merge-write config to $HERMES_HOME/mem0.json."""
        from utils import atomic_json_write
        config_path = Path(hermes_home) / "mem0.json"
        atomic_json_write(config_path, {**_read_mem0_json(config_path), **values}, mode=0o600)

    def get_config_schema(self):
        api_key_required = _load_config().get("mode", "platform") != "oss"
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

    def _oss_hint(self, template: str, default: str = "vector store") -> str:
        """OSS-only hint; ``{vs}`` is the configured vector-store provider. "" in other modes."""
        return template.format(vs=self._config.get("oss", {}).get("vector_store", {}).get("provider", default)) if self._mode == "oss" else ""

    def _create_backend(self):
        # Lazy-install the mem0 SDK before the backend imports it (honors security.allow_lazy_installs);
        # on failure the backend import raises the canonical error, captured below.
        with suppress(Exception):
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("memory.mem0", prompt=False)
        try:
            from . import _backend
            if self._mode == "oss":
                return _backend.OSSBackend(self._config.get("oss", {}))
            return _backend.SelfHostedBackend(self._api_key, self._host) if self._host else _backend.PlatformBackend(self._api_key)
        except Exception as e:
            logger.error("Mem0 backend failed to initialize (%s mode): %s", self._mode, e)
            self._init_error = str(e)
            return None

    def _is_breaker_open(self) -> bool:
        """True while the breaker is tripped; an expired cooldown resets the failure count."""
        with self._breaker_lock:
            if self._consecutive_failures >= _BREAKER_THRESHOLD and time.monotonic() < self._breaker_open_until:
                return True
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._consecutive_failures = 0
            return False

    def _format_error(self, prefix: str, exc: Exception) -> str:
        msg = f"{prefix}: {exc}"
        if any(s in str(exc).lower() for s in ("connection", "refused", "timeout")):
            msg += self._oss_hint(" (check that {vs} is running)")
        return msg

    def _record_success(self):
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._breaker_lock:
            self._consecutive_failures = count = self._consecutive_failures + 1
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
        if count >= _BREAKER_THRESHOLD:
            hint = self._oss_hint(" Check that your {vs} vector store is running and reachable.", "unknown")
            logger.warning("Mem0 circuit breaker tripped after %d consecutive failures. Pausing API calls for %ds.%s", count, _BREAKER_COOLDOWN_SECS, hint)

    def _try(self, call, log, msg: str):
        """Background-path wrapper: run ``call`` under the breaker; on error log ``msg`` and return None."""
        try:
            result = call()
        except Exception as e:
            self._record_failure()
            log(msg, e)
            return None
        self._record_success()
        return result

    def initialize(self, session_id: str, **kwargs) -> None:
        # Invalidate old sync workers before changing any session/profile state.
        # Workers capture their launch context and check this token before they
        # may mutate breaker/replay state.
        with self._sync_state_lock:
            self._sync_gen += 1
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
        # Local patch: per-message sync char cap (configurable via mem0.json sync_max_chars).
        self._sync_max_chars = int(self._config.get("sync_max_chars") or _SYNC_MSG_MAX_CHARS)
        # Instance may be re-initialized after teardown (cached providers):
        # reset teardown/session state so a leftover prefetch result from the
        # previous session is never injected into the new one.
        self._shutting_down = False
        with self._prefetch_lock:
            self._prefetch_query = ""
            self._prefetch_result = ""
            self._prefetch_done = False
            self._prefetch_gen += 1  # invalidate any in-flight prefetch write
        self._prefetch_thread = None
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        if not self._hermes_home:
            from hermes_constants import get_hermes_home
            self._hermes_home = str(get_hermes_home())
        self._backend = self._create_backend()
        if self._backend and not self._atexit_registered:
            atexit.register(self._shutdown_backend)
            self._atexit_registered = True

    def _search(self, query: str, top_k: int = 10, rerank: bool = False, backend=None) -> list:
        # Scoped to user_id only — by design — so recall surfaces memories from any gateway/agent under this
        # principal; writes attach agent_id and metadata.channel so narrower views remain possible at query time.
        return (backend or self._backend).search(query, filters={"user_id": self._user_id}, top_k=top_k, rerank=rerank)

    def _add(self, messages: list, infer: bool):
        metadata = {"channel": self._channel} if self._channel else {}
        return self._backend.add(messages, user_id=self._user_id, agent_id=self._agent_id, infer=infer, metadata=metadata)

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
        # Mirror _create_backend precedence (oss > host > platform). Rerank is a Mem0 Platform feature only.
        mode_label = "OSS (self-hosted)" if self._mode == "oss" else "self-hosted (HTTP API)" if self._host else "platform (cloud API)"
        rerank_note = " Rerank is available on search." if (self._mode == "platform" and not self._host) else ""
        return f"# Mem0 Memory\nActive. Mode: {mode_label}. User: {self._user_id}.\n{_PROMPT_BODY}{rerank_note}"

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def _consume_prefetch_result(self, query: str) -> str | None:
        """Pop the finished prefetch body for ``query`` (None if absent or still running)."""
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result, self._prefetch_result, self._prefetch_done = self._prefetch_result, "", False
            return result

    def _start_prefetch(self, query: str) -> None:
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
            self._prefetch_gen += 1
            gen = self._prefetch_gen

        def _run():
            body = ""
            failure = None
            try:
                results = backend.search(
                    query, filters=self._read_filters(), top_k=10, rerank=False,
                )
                lines = [r.get("memory", "") for r in (results or []) if r.get("memory")]
                if lines:
                    body = "## Mem0 Memory\n" + "\n".join(f"- {l}" for l in lines)
            except Exception as e:
                failure = e
                logger.debug("Mem0 prefetch failed: %s", e)
            with self._prefetch_lock:
                if gen != self._prefetch_gen or self._prefetch_query != query:
                    return
                if failure is None:
                    self._record_success()
                else:
                    self._record_failure()
                self._prefetch_result = body
                self._prefetch_done = True

        with self._prefetch_lock:
            # Same query already answered or still in flight: don't restart it.
            if self._prefetch_query == query and (self._prefetch_done or (self._prefetch_thread and self._prefetch_thread.is_alive())):
                return
            self._prefetch_query, self._prefetch_result, self._prefetch_done = query, "", False
            self._prefetch_thread = t = threading.Thread(target=_run, daemon=True, name="mem0-prefetch")
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        if (cached := self._consume_prefetch_result(query)) is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        return self._consume_prefetch_result(query) or ""  # slow backend: skip injection; mem0_search remains the backstop

    # -- Dead-letter queue ----------------------------------------------------

    def _deadletter_path(self):
        if self._hermes_home:
            return Path(self._hermes_home) / "state" / "mem0-deadletter.jsonl"
        # fallback for uninitialized instances (tests) — env-based, so safe
        # to resolve off-thread
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "state" / "mem0-deadletter.jsonl"

    # Splitting is on "\n" only, never str.splitlines(): json.dumps with
    # ensure_ascii=False leaves U+2028/U+2029/U+0085 unescaped inside entries,
    # and splitlines() would fragment those entries into "corrupt" pieces.
    @staticmethod
    def _split_entries(raw: str) -> list:
        return [l for l in raw.split("\n") if l.strip()]

    @staticmethod
    def _fsync_parent(path) -> None:
        """Persist a created/replaced directory entry where supported."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(path.parent, flags)
        except OSError:
            return  # Windows and some filesystems do not allow directory fsync
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _deadletter_write(path, lines: list) -> None:
        """Atomically replace the queue file (caller holds the locks).

        fsync before the rename — without it a power loss shortly after the
        rename can leave an empty/zero-filled file on writeback filesystems,
        losing the whole queue in one shot.
        """
        tmp = path.with_suffix(".jsonl.tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(("\n".join(lines) + "\n") if lines else "")
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
        Mem0MemoryProvider._fsync_parent(path)

    @staticmethod
    def _deadletter_bound(lines: list) -> list:
        """Bound the queue by entry count, then by bytes (keep newest)."""
        lines = lines[-_DEADLETTER_MAX:]
        total = sum(len(l.encode("utf-8")) + 1 for l in lines)
        # trim to 3/4 of the trigger threshold — without hysteresis every
        # append at saturation rewrites the whole file
        target = _DEADLETTER_TRIM_BYTES * 3 // 4
        while len(lines) > 1 and total > target:
            total -= len(lines.pop(0).encode("utf-8")) + 1
        return lines

    def _turn_messages(self, user_content: str, assistant_content: str) -> list:
        # Local patch: cap each message — OSS embedders have small context
        # windows and oversized turns make backend.add() raise, silently
        # dropping the turn's memory extraction.
        return [
            {"role": "user", "content": _truncate_for_sync(user_content, self._sync_max_chars)},
            {"role": "assistant", "content": _truncate_for_sync(assistant_content, self._sync_max_chars)},
        ]

    def _deadletter_append(self, user_content: str, assistant_content: str,
                           ts: float = 0.0, *, hermes_home: str | None = None,
                           user_id: str | None = None,
                           agent_id: str | None = None,
                           metadata: dict | None = None) -> bool:
        """Queue a turn whose sync was dropped; True if durably queued.

        ``ts`` is the TURN timestamp (stamped when sync_turn was called, in
        conversation order) — queue-time would invert replay order when a
        hung-then-failed sync appends an older turn after a busy-skipped
        newer one, regressing corrected facts.
        """
        queued = False
        try:
            entry = json.dumps({
                "ts": ts or time.time(),
                "messages": self._turn_messages(user_content, assistant_content),
                "user_id": self._user_id if user_id is None else user_id,
                "agent_id": self._agent_id if agent_id is None else agent_id,
                "metadata": self._write_metadata() if metadata is None else metadata,
            }, ensure_ascii=False)
            with self._deadletter_lock:
                path = (
                    Path(hermes_home) / "state" / "mem0-deadletter.jsonl"
                    if hermes_home else self._deadletter_path()
                )
                parent_existed = path.parent.exists()
                path.parent.mkdir(parents=True, exist_ok=True)
                if not parent_existed:
                    self._fsync_parent(path.parent)
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
                    fd = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                        0o600,
                    )
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
                        if needs_nl:
                            f.write("\n")
                        f.write(entry + "\n")
                        f.flush()
                        # fsync: "queued for replay" must survive a power loss
                        # — same durability the rewrite path already provides
                        os.fsync(f.fileno())
                    self._fsync_parent(path)
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
                before = list(lines)
                for old, new in (replace or {}).items():
                    if old in lines:
                        lines[lines.index(old)] = new
                for line in remove:
                    if line in lines:
                        lines.remove(line)
                if lines != before:
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
            # A drain hung on a backend call without a timeout would silently
            # block draining forever (and hold the cross-process lock) — give
            # the operator a signal, roughly hourly.
            if time.monotonic() - self._replay_started_at > 3600:
                self._replay_started_at = time.monotonic()
                logger.warning(
                    "Mem0 dead-letter drain has been running for over an hour — "
                    "possibly hung on a backend call; queued turns are not draining.")
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

        try:
            self._replay_thread = threading.Thread(
                target=_drain, daemon=True, name="mem0-replay")
            self._replay_thread.start()
            self._replay_started_at = time.monotonic()
        except Exception as e:
            # Thread.start() raises RuntimeError at interpreter finalization
            # (or thread exhaustion) — must not kill the sync worker; the
            # queue drains on a later sync or the next process.
            logger.debug("Mem0 dead-letter drain could not start: %s", e)

    @staticmethod
    def _try_drain_lock(lock_path):
        """Acquire the cross-process drain lock without blocking.

        Returns an open file handle holding the platform lock (close to release),
        or None if another process's drain holds it.
        """
        f = lock_path.open("a+b")
        if fcntl is None:
            if msvcrt is None:
                f.close()
                return None
            try:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    f.write(b"\0")
                    f.flush()
                f.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return f
            except OSError:
                f.close()
                return None
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
                    if not (isinstance(messages, list)
                            and all(isinstance(m, dict) for m in messages)):
                        raise ValueError("malformed messages")
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
                if self._shutting_down or backend is not self._backend:
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
                        # A failure against a closed/replaced backend (shutdown,
                        # or re-init swapping self._backend under a hung drain)
                        # is not a replay attempt.
                        if self._shutting_down or backend is not self._backend:
                            return
                        attempts = (entry.get("attempts") or 0) + 1
                        if attempts >= _DEADLETTER_MAX_ATTEMPTS:
                            logger.warning(
                                "Mem0 dead-letter entry failed %d replay attempts — dropping it: %s",
                                attempts, e)
                            self._deadletter_mutate(remove=[line])
                            continue
                        # Attempts can tick up on the oldest entry during a
                        # flaky-backend window (sync succeeded, replay add
                        # failed): allow 8 healthy-sync failures before calling
                        # one turn poisoned is the accepted ceiling
                        entry["attempts"] = attempts
                        self._deadletter_mutate(
                            replace={line: json.dumps(entry, ensure_ascii=False)})
                        logger.warning(
                            "Mem0 dead-letter replay paused (%d turns queued, attempt %d): %s",
                            len(entries), attempts, e,
                        )
                        return
                # initialize() can replace the backend while an old replay add
                # is in flight. The old side effect may have happened, but it
                # must never remove the shared queue entry or continue draining
                # newer entries for the replacement backend.
                if self._shutting_down or backend is not self._backend:
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
        # Use content annotation because OSS Memory.add rejects the timestamp
        # param (platform-only); switch to timestamp= if OSS mem0 supports it
        # later or the annotation proves too weak.
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

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages=None) -> None:
        """Send the turn to Mem0 for server-side fact extraction (non-blocking)."""
        turn_ts = time.time()  # conversation-order stamp for any queued copy

        with self._sync_state_lock:
            backend = self._backend
            launch_gen = self._sync_gen
            launch_home = self._hermes_home
            launch_user_id = self._user_id
            launch_agent_id = self._agent_id
            launch_metadata = self._write_metadata()

        def _queue_launch_turn() -> bool:
            return self._deadletter_append(
                user_content,
                assistant_content,
                ts=turn_ts,
                hermes_home=launch_home,
                user_id=launch_user_id,
                agent_id=launch_agent_id,
                metadata=launch_metadata,
            )

        if backend is None or self._is_breaker_open():
            # Backend down or breaker open: queue instead of losing the turn.
            _queue_launch_turn()
            return

        def _sync():
            try:
                backend.add(
                    self._turn_messages(user_content, assistant_content),
                    user_id=launch_user_id,
                    agent_id=launch_agent_id,
                    infer=True,
                    metadata=launch_metadata,
                )
            except Exception as e:
                # Queue against the launch-time profile and identity even if
                # initialize() has already repurposed this provider instance.
                with self._sync_state_lock:
                    current = (
                        launch_gen == self._sync_gen
                        and backend is self._backend
                    )
                    if current:
                        self._record_failure()
                if _queue_launch_turn():
                    logger.warning("Mem0 sync failed (turn queued for replay): %s", e)
                else:
                    logger.warning("Mem0 sync failed and turn could not be queued: %s", e)
                return

            # A stale worker's backend side effect may have completed, but it
            # must not mutate the replacement session's breaker/replay state.
            with self._sync_state_lock:
                if launch_gen != self._sync_gen or backend is not self._backend:
                    return
                self._record_success()
                self._start_deadletter_replay(backend)

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=5.0)
            # If still alive after timeout, queue the turn (previously it was
            # skipped outright "to avoid duplicate ingestion" — i.e. dropped);
            # the in-flight sync's replay pass will pick it up.
            if self._sync_thread and self._sync_thread.is_alive():
                _queue_launch_turn()
                return
            self._sync_thread = threading.Thread(target=_sync, daemon=True, name="mem0-sync")
            self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(TOOL_SCHEMAS)

    # -- tool handlers: (required params, error label, body, client-error policy) ---
    # Client errors (bad ID / not found) never trip the breaker, except for mem0_add
    # where they count as failures; update/delete answer them with "Memory not found".

    def _tool_search(self, args: dict) -> str:
        top_k = max(1, min(int(args.get("top_k", 10)), 50))
        rerank_raw = args.get("rerank", self._rerank_default)
        rerank = rerank_raw.lower() not in ("false", "0", "no") if isinstance(rerank_raw, str) else bool(rerank_raw)
        results = self._search(args["query"], top_k, rerank)
        if not results:
            return json.dumps({"result": "No relevant memories found."})
        items = [{"id": r.get("id"), "memory": r.get("memory", ""), "score": r.get("score", 0)} for r in results]
        return json.dumps({"results": items, "count": len(items)})

    def _tool_add(self, args: dict) -> str:
        result = self._add([{"role": "user", "content": args["content"]}], infer=False)
        event_id = result.get("event_id") if isinstance(result, dict) else None
        # Cloud add is async (server-side extraction); OSS and self-hosted store synchronously.
        msg = "Fact stored." if (self._mode == "oss" or self._host) else "Fact queued for storage."
        return json.dumps({"result": msg, "event_id": event_id})

    _TOOL_HANDLERS = {
        "mem0_search": (("query",), "Search failed", _tool_search, "skip"),
        "mem0_add": (("content",), "Failed to store", _tool_add, "count"),
        "mem0_update": (("memory_id", "text"), "Update failed", lambda self, a: json.dumps(self._backend.update(a["memory_id"], a["text"])), "not_found"),
        "mem0_delete": (("memory_id",), "Delete failed", lambda self, a: json.dumps(self._backend.delete(a["memory_id"])), "not_found"),
    }

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._backend is None:
            err = getattr(self, "_init_error", "unknown error")
            return json.dumps({"error": f"Mem0 backend not initialized: {err}.{self._oss_hint(' Check that {vs} is running and reachable.')}"})
        if self._is_breaker_open():
            return json.dumps({"error": f"Mem0 temporarily unavailable (multiple consecutive failures). Will retry automatically.{self._oss_hint(' Check that your {vs} is running.')}"})
        if tool_name not in self._TOOL_HANDLERS:
            return tool_error(f"Unknown tool: {tool_name}")
        required, label, body, on_client_error = self._TOOL_HANDLERS[tool_name]
        if missing := next((k for k in required if not args.get(k, "")), None):
            return tool_error(f"Missing required parameter: {missing}")
        try:
            result = body(self, args)
        except Exception as e:
            client = _is_client_error(e)
            if client and on_client_error == "not_found":
                return tool_error(f"Memory not found: {args['memory_id']}")
            if not client or on_client_error == "count":
                self._record_failure()
            return tool_error(self._format_error(label, e))
        self._record_success()
        return result

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
        deadline = time.monotonic() + 30.0
        # The final write is the only must-preserve operation. Replay and
        # read-only prefetch share whatever remains of the same shutdown budget.
        def join(t):
            if t and t.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    t.join(timeout=remaining)

        join(self._sync_thread)
        join(self._replay_thread)  # sync may have published this while joining
        join(self._prefetch_thread)
        self._shutdown_backend()


def register(ctx) -> None:
    """Register Mem0 as a memory provider plugin."""
    ctx.register_memory_provider(Mem0MemoryProvider())


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

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
# ---- END PLUGIN-COMPAT ----
