import os
import shlex

from tools.code_execution_tool import (
    _scrub_child_env,
    _child_cache_redirect_env,
    _sandbox_cache_env_prefix,
)


def test_scrub_drops_cache_vars():
    # The allowlist must DROP cache vars (none match _SAFE_ENV_PREFIXES); this
    # is exactly why post-scrub injection is required.
    src = {
        "PYTHONPYCACHEPREFIX": "/work/pycache",
        "RUFF_CACHE_DIR": "/work/ruff",
        "MYPY_CACHE_DIR": "/work/mypy",
        "PYTEST_ADDOPTS": "--cache-clear",
        "npm_config_cache": "/work/npm",
        "PATH": "/usr/bin",
    }
    scrubbed = _scrub_child_env(src, is_passthrough=lambda _: False, is_windows=False)
    for k in ("PYTHONPYCACHEPREFIX", "RUFF_CACHE_DIR", "MYPY_CACHE_DIR",
              "PYTEST_ADDOPTS", "npm_config_cache"):
        assert k not in scrubbed, f"{k} should be dropped by the scrub"
    assert scrubbed.get("PATH") == "/usr/bin"  # sanity: safe var survives


def test_child_cache_redirect_env_points_under_hermes_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = _child_cache_redirect_env()
    expected_base = os.path.join(str(tmp_path), "scratch", "caches")
    assert env["PYTHONPYCACHEPREFIX"].startswith(expected_base)
    assert os.path.isabs(env["RUFF_CACHE_DIR"])
    assert "npm_config_cache" in env and "PYTEST_ADDOPTS" in env


def test_injection_restores_cache_vars_after_scrub(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scrubbed = _scrub_child_env(
        {"PYTHONPYCACHEPREFIX": "/work/pycache", "PATH": "/usr/bin"},
        is_passthrough=lambda _: False, is_windows=False,
    )
    assert "PYTHONPYCACHEPREFIX" not in scrubbed  # dropped
    scrubbed.update(_child_cache_redirect_env())   # production does this
    assert scrubbed["PYTHONPYCACHEPREFIX"].startswith(str(tmp_path))
    assert os.path.join("scratch", "caches") in scrubbed["PYTHONPYCACHEPREFIX"]


def test_sandbox_cache_env_prefix_is_shell_quoted_and_sandbox_scoped():
    s = _sandbox_cache_env_prefix("/tmp/sbx with space")
    tokens = shlex.split(s)  # must parse cleanly despite the space
    assert len(tokens) == 3
    keys = {t.split("=", 1)[0] for t in tokens}
    assert keys == {"PYTHONPYCACHEPREFIX", "MYPY_CACHE_DIR", "RUFF_CACHE_DIR"}
    for t in tokens:
        val = t.split("=", 1)[1]
        assert val.startswith("/tmp/sbx with space/.caches/")
