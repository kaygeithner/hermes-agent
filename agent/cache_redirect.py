"""Redirect tool caches to an absolute scratch dir so they never land in
the cwd (e.g. ~/work/). The dominant clutter vector is the bash/terminal
path, which inherits os.environ unscrubbed; setting these on the service
process covers it. code_execution_tool injects the same set post-scrub."""
import os


def cache_redirect_env(base_dir: str) -> dict[str, str]:
    """Return cache env vars pointing under an ABSOLUTE base_dir."""
    base = os.path.abspath(base_dir)
    return {
        "PYTHONPYCACHEPREFIX": os.path.join(base, "pycache"),
        "MYPY_CACHE_DIR": os.path.join(base, "mypy"),
        "RUFF_CACHE_DIR": os.path.join(base, "ruff"),
        # Disable pytest's .pytest_cache rather than relocate (simplest, and the
        # cwd is no longer work/ once redirected). Best-effort: repos may override.
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "npm_config_cache": os.path.join(base, "npm"),
    }


def apply_cache_redirect_defaults(base_dir: str) -> None:
    """setdefault each redirect var (never clobber an explicit value) and
    ensure base_dir exists."""
    os.makedirs(os.path.abspath(base_dir), exist_ok=True)
    for k, v in cache_redirect_env(base_dir).items():
        os.environ.setdefault(k, v)


def apply_cache_redirect_from_hermes_home() -> None:
    """Redirect tool caches under ``$HERMES_HOME/scratch/caches``.

    Called once at startup from ``hermes_cli.main`` immediately after the
    profile override resolves HERMES_HOME. Reading HERMES_HOME here (rather
    than at import time) ensures the active profile's home wins. Falls back to
    ``~/.hermes`` when HERMES_HOME is unset (the default-profile case)."""
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    apply_cache_redirect_defaults(os.path.join(hermes_home, "scratch", "caches"))
