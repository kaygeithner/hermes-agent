"""Redirect tool caches to an absolute scratch dir so they never land in
the cwd (e.g. ~/work/). The dominant clutter vector is the bash/terminal
path, which inherits os.environ unscrubbed; setting these on the service
process covers it. code_execution_tool injects the same set post-scrub."""
import logging
import os
import shlex

logger = logging.getLogger(__name__)

# Single source of truth for the env vars cache_redirect_env relocates. The
# test-isolation fixture (tests/conftest.py) and the code-exec child injection
# consume this so they can never drift from the producer below.
CACHE_ENV_VARS: tuple[str, ...] = (
    "PYTHONPYCACHEPREFIX",
    "MYPY_CACHE_DIR",
    "RUFF_CACHE_DIR",
    "PYTEST_ADDOPTS",
    "npm_config_cache",
)


def hermes_home_cache_base() -> str:
    """Absolute cache-scratch base under HERMES_HOME (falls back to ~/.hermes).

    Single source of the base path so the startup hook and the code-exec child
    injection stay in agreement on where caches go."""
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(hermes_home, "scratch", "caches")


def cache_redirect_env(base_dir: str, *, include_pycache: bool = True) -> dict[str, str]:
    """Return cache env vars pointing under an ABSOLUTE base_dir.

    Every cache is RELOCATED, not disabled, so cache-backed features keep
    working — pytest in particular keeps --lf/--ff/--sw because we move its
    cache_dir via -o rather than switching the cache plugin off.

    ``include_pycache=False`` drops PYTHONPYCACHEPREFIX for callers that run the
    target with PYTHONDONTWRITEBYTECODE=1 (the code-exec child): no __pycache__
    is ever written, so the prefix is a dead no-op. Keeping the omission in the
    producer means those callers don't each have to remember to pop the key."""
    base = os.path.abspath(base_dir)
    env = {
        "MYPY_CACHE_DIR": os.path.join(base, "mypy"),
        "RUFF_CACHE_DIR": os.path.join(base, "ruff"),
        # Relocate pytest's cache_dir (the only one with no dedicated env var)
        # via PYTEST_ADDOPTS. shlex.quote keeps it intact when base has spaces
        # (pytest parses PYTEST_ADDOPTS with shlex). This -o overrides a repo's
        # ini cache_dir; the escape hatch is an explicit PYTEST_ADDOPTS, which
        # setdefault leaves untouched.
        "PYTEST_ADDOPTS": "-o " + shlex.quote("cache_dir=" + os.path.join(base, "pytest")),
        "npm_config_cache": os.path.join(base, "npm"),
    }
    if include_pycache:
        env["PYTHONPYCACHEPREFIX"] = os.path.join(base, "pycache")
    return env


def apply_cache_redirect_defaults(base_dir: str) -> None:
    """setdefault each redirect var (never clobber an explicit value) and
    ensure base_dir exists.

    Best-effort: any failure (unwritable/misconfigured HERMES_HOME, read-only
    fs, disk full, a path component that is a file, ...) is swallowed so this
    cache-tidiness step can NEVER crash a Hermes entry point at startup — it
    degrades to the prior behavior (caches land in the cwd). Mirrors the
    defensive style of _apply_profile_override() in hermes_cli.main."""
    try:
        abs_base = os.path.abspath(base_dir)
        os.makedirs(abs_base, exist_ok=True)
        for k, v in cache_redirect_env(abs_base).items():
            os.environ.setdefault(k, v)
    except Exception:
        logger.debug(
            "cache redirect skipped (could not prepare %s)", base_dir, exc_info=True
        )


def apply_cache_redirect_from_hermes_home() -> None:
    """Redirect tool caches under ``$HERMES_HOME/scratch/caches``.

    Called once at startup from ``hermes_cli.main`` immediately after the
    profile override resolves HERMES_HOME. Reading HERMES_HOME here (rather
    than at import time) ensures the active profile's home wins. Falls back to
    ``~/.hermes`` when HERMES_HOME is unset (the default-profile case).
    Best-effort (see apply_cache_redirect_defaults) — never raises into the
    import path."""
    apply_cache_redirect_defaults(hermes_home_cache_base())
