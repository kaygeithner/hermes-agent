"""Redirect tool caches away from the agent's working directory."""

import logging
import os
import shlex

logger = logging.getLogger(__name__)


def hermes_home_cache_base() -> str:
    """Return the absolute cache scratch root for the active Hermes profile."""
    # get_hermes_home() also honors the context-local override used by
    # in-process profile routing. Reading only HERMES_HOME would redirect a
    # named profile's child-process caches into the default profile.
    from hermes_constants import get_hermes_home

    return os.path.join(os.fspath(get_hermes_home()), "scratch", "caches")


def cache_redirect_env(base_dir: str) -> dict[str, str]:
    """Return cache variables relocated beneath an absolute base directory."""
    base = os.path.abspath(base_dir)
    return {
        "PYTHONPYCACHEPREFIX": os.path.join(base, "pycache"),
        "MYPY_CACHE_DIR": os.path.join(base, "mypy"),
        "RUFF_CACHE_DIR": os.path.join(base, "ruff"),
        "PYTEST_ADDOPTS": "-o "
        + shlex.quote("cache_dir=" + os.path.join(base, "pytest")),
        "npm_config_cache": os.path.join(base, "npm"),
    }


def apply_cache_redirect_defaults(base_dir: str) -> None:
    """Set missing cache variables, degrading safely if the base is unusable."""
    try:
        abs_base = os.path.abspath(base_dir)
        os.makedirs(abs_base, exist_ok=True)
        for key, value in cache_redirect_env(abs_base).items():
            os.environ.setdefault(key, value)
    except Exception:
        logger.debug(
            "cache redirect skipped (could not prepare %s)", base_dir, exc_info=True
        )


def apply_cache_redirect_from_hermes_home() -> None:
    """Apply cache defaults after the active profile resolves HERMES_HOME."""
    apply_cache_redirect_defaults(hermes_home_cache_base())
