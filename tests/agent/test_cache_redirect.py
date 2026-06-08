import os
from agent.cache_redirect import cache_redirect_env, apply_cache_redirect_defaults

def test_cache_redirect_env_uses_absolute_paths_under_base(tmp_path):
    env = cache_redirect_env(str(tmp_path))
    # All redirected caches must be ABSOLUTE and under the given base dir.
    for key in ("PYTHONPYCACHEPREFIX", "MYPY_CACHE_DIR", "RUFF_CACHE_DIR"):
        assert key in env
        assert os.path.isabs(env[key])
        assert str(tmp_path) in env[key]
    # pytest cache disabled OR redirected; npm cache redirected.
    assert "PYTEST_ADDOPTS" in env
    assert "npm_config_cache" in env

def test_apply_defaults_does_not_clobber_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("RUFF_CACHE_DIR", "/already/set")
    apply_cache_redirect_defaults(str(tmp_path))
    assert os.environ["RUFF_CACHE_DIR"] == "/already/set"          # respected
    assert os.path.isabs(os.environ["PYTHONPYCACHEPREFIX"])         # newly set


def test_apply_from_hermes_home_redirects_under_scratch_caches(tmp_path, monkeypatch):
    from agent.cache_redirect import apply_cache_redirect_from_hermes_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Ensure a clean slate for the vars this sets via setdefault.
    for var in ("PYTHONPYCACHEPREFIX", "MYPY_CACHE_DIR", "RUFF_CACHE_DIR",
                "PYTEST_ADDOPTS", "npm_config_cache"):
        monkeypatch.delenv(var, raising=False)

    apply_cache_redirect_from_hermes_home()

    expected_base = os.path.join(str(tmp_path), "scratch", "caches")
    assert os.path.isabs(os.environ["PYTHONPYCACHEPREFIX"])
    assert os.path.join("scratch", "caches") in os.environ["PYTHONPYCACHEPREFIX"]
    assert os.environ["PYTHONPYCACHEPREFIX"].startswith(expected_base)
    assert os.path.isdir(expected_base)  # makedirs ran
