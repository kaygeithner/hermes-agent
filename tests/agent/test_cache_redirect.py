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
