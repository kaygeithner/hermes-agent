import os
import shlex

import pytest

from agent.cache_redirect import cache_redirect_env, apply_cache_redirect_defaults

_CACHE_VARS = (
    "PYTHONPYCACHEPREFIX",
    "MYPY_CACHE_DIR",
    "RUFF_CACHE_DIR",
    "PYTEST_ADDOPTS",
    "npm_config_cache",
)


@pytest.fixture(autouse=True)
def _isolate_cache_env():
    saved = {key: os.environ.get(key) for key in _CACHE_VARS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_cache_redirect_env_uses_absolute_paths_under_base(tmp_path):
    env = cache_redirect_env(str(tmp_path))
    for key in ("PYTHONPYCACHEPREFIX", "MYPY_CACHE_DIR", "RUFF_CACHE_DIR"):
        assert os.path.isabs(env[key])
        assert env[key].startswith(str(tmp_path))
    assert shlex.split(env["PYTEST_ADDOPTS"])[1].startswith("cache_dir=")
    assert env["npm_config_cache"].startswith(str(tmp_path))


def test_apply_defaults_does_not_clobber_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("RUFF_CACHE_DIR", "/already/set")
    apply_cache_redirect_defaults(str(tmp_path))
    assert os.environ["RUFF_CACHE_DIR"] == "/already/set"
    assert os.path.isabs(os.environ["PYTHONPYCACHEPREFIX"])


def test_apply_from_active_hermes_home(tmp_path, monkeypatch):
    from agent.cache_redirect import apply_cache_redirect_from_hermes_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in _CACHE_VARS:
        monkeypatch.delenv(var, raising=False)
    apply_cache_redirect_from_hermes_home()

    expected = os.path.join(str(tmp_path), "scratch", "caches")
    assert os.environ["PYTHONPYCACHEPREFIX"].startswith(expected)
    assert os.path.isdir(expected)


def test_apply_honors_context_local_profile_home(tmp_path, monkeypatch):
    from agent.cache_redirect import apply_cache_redirect_from_hermes_home
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    process_home = tmp_path / "default"
    profile_home = tmp_path / "profiles" / "work"
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    for var in _CACHE_VARS:
        monkeypatch.delenv(var, raising=False)

    token = set_hermes_home_override(profile_home)
    try:
        apply_cache_redirect_from_hermes_home()
    finally:
        reset_hermes_home_override(token)

    expected = profile_home / "scratch" / "caches"
    assert os.environ["PYTHONPYCACHEPREFIX"].startswith(str(expected))
    assert expected.is_dir()


def test_apply_defaults_degrades_when_base_cannot_be_prepared(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    for var in _CACHE_VARS:
        monkeypatch.delenv(var, raising=False)

    apply_cache_redirect_defaults(str(blocker / "scratch" / "caches"))

    assert all(var not in os.environ for var in _CACHE_VARS)
