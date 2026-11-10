"""P7: the chat's last model pick per gateway session key, for turns the gateway starts itself.

Gateway-injected turns (completion/watch wakes, internal events) carry no inbound metadata, so
they would run on the config default model inside a chat whose pick is another model. The relay
hub sends the pick per inbound message (P6); this module remembers it per session key so injected
turns inherit it. The pick survives a restart (JSON file under the Hermes home), is cleared when
a later human inbound for the same session key carries no alias, and still goes through the
unchanged P6 eligibility check + warning in TurnRunner.run_sync.
"""

import json
import logging
import threading
from typing import Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("gateway.run")

_PICKS_FILE = ("gateway", "relay_model_picks.json")  # under get_hermes_home(), resolved at call time
_lock = threading.Lock()
# ponytail: one in-memory copy per Hermes home, so a turn never reads the file; only a pick change writes it
_cache: tuple = (None, {})


def _picks_path():
    return get_hermes_home().joinpath(*_PICKS_FILE)


def _load_picks() -> dict:
    """Caller holds ``_lock``. The file is read once per Hermes home, then served from memory."""
    global _cache
    path = _picks_path()
    if _cache[0] != path:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            data = {}
        except ValueError:
            data = {}  # corrupt file: the next pick change rewrites it
        # any other OSError (EMFILE, EACCES) propagates uncached: a transient read error must not
        # make the next remember() overwrite every other chat's pick
        _cache = (path, data if isinstance(data, dict) else {})
    return _cache[1]


def remember(session_key: str, alias: Optional[str]) -> None:
    """Store the chat's pick for ``session_key`` (delete on a falsy alias); never raises."""
    global _cache
    if not isinstance(session_key, str) or not session_key:
        return
    alias = alias if isinstance(alias, str) and alias else None
    try:
        with _lock:
            picks = dict(_load_picks())  # a copy: a failed write leaves the cache as the file, so the next inbound retries
            if alias:
                if picks.get(session_key) == alias:
                    return
                picks[session_key] = alias
            else:
                if session_key not in picks:
                    return
                del picks[session_key]
            atomic_json_write(_picks_path(), picks)
            _cache = (_cache[0], picks)
    except OSError as e:
        logger.warning("relay model pick persist failed for %s: %s", session_key, e)


def recall(session_key: str) -> Optional[str]:
    """The stored alias (str) or None; never raises; non-str values are ignored."""
    if not isinstance(session_key, str) or not session_key:
        return None
    try:
        with _lock:
            alias = _load_picks().get(session_key)
    except OSError as e:
        logger.warning("relay model pick read failed for %s: %s", session_key, e)
        return None
    return alias if isinstance(alias, str) else None


def turn_model_alias(event, session_key: Optional[str]) -> Optional[str]:
    """Resolve the model alias for a turn: the event's own pick, or the chat's remembered one."""
    alias = (getattr(event, "metadata", None) or {}).get("model_alias")
    if event is None or not isinstance(session_key, str) or not session_key:
        return alias
    # gateway-started turns (internal wakes, goal continuations, heartbeats) read the pick, never clear it
    if (getattr(event, "internal", False) or getattr(event, "inherit_channel_pin", False)
            or getattr(event, "_heartbeat_session_id", None)):
        return alias or recall(session_key)
    remember(session_key, alias)
    return alias
