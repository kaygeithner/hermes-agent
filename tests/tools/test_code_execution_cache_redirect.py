import os
import shlex

from tools.code_execution_tool import (
    _apply_child_cache_redirect_defaults,
    _child_cache_redirect_env,
    _sandbox_cache_env_prefix,
    _scrub_child_env,
)


def test_scrub_then_default_redirects_child_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = _scrub_child_env(
        {"PYTHONPYCACHEPREFIX": "/work/pycache", "PATH": "/usr/bin"},
        is_passthrough=lambda _: False,
        is_windows=False,
    )
    assert "PYTHONPYCACHEPREFIX" not in child

    _apply_child_cache_redirect_defaults(child)

    expected = os.path.join(str(tmp_path), "scratch", "caches")
    assert child["PYTHONPYCACHEPREFIX"].startswith(expected)
    assert child["RUFF_CACHE_DIR"].startswith(expected)


def test_child_redirect_preserves_explicit_passthrough(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = _scrub_child_env(
        {"RUFF_CACHE_DIR": "/explicit/ruff"},
        is_passthrough=lambda key: key == "RUFF_CACHE_DIR",
        is_windows=False,
    )

    _apply_child_cache_redirect_defaults(child)

    assert child["RUFF_CACHE_DIR"] == "/explicit/ruff"
    assert child["MYPY_CACHE_DIR"].startswith(str(tmp_path))


def test_child_redirect_degrades_when_base_is_unusable(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fail_makedirs(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr("os.makedirs", fail_makedirs)
    child = {"PYTHONDONTWRITEBYTECODE": "1"}
    assert _child_cache_redirect_env() == {}
    _apply_child_cache_redirect_defaults(child)
    assert child == {"PYTHONDONTWRITEBYTECODE": "1"}


def test_remote_sandbox_redirect_is_quoted_and_sandbox_scoped():
    tokens = shlex.split(_sandbox_cache_env_prefix("/tmp/sbx with space"))
    assert {token.split("=", 1)[0] for token in tokens} == {
        "PYTHONPYCACHEPREFIX",
        "MYPY_CACHE_DIR",
        "RUFF_CACHE_DIR",
    }
    assert all(token.split("=", 1)[1].startswith("/tmp/sbx with space/.caches/") for token in tokens)
