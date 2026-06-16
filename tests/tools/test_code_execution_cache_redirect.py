import os
import shlex

from agent.cache_redirect import CACHE_ENV_VARS
from tools.code_execution_tool import (
    _child_cache_redirect_env,
    _inject_child_cache_redirect,
    _sandbox_cache_env_prefix,
    _scrub_child_env,
)


def test_scrub_drops_cache_vars():
    # The allowlist must DROP cache vars (none match _SAFE_ENV_PREFIXES); this
    # is exactly why post-scrub injection is required.
    src = {var: f"/work/{var.lower()}" for var in CACHE_ENV_VARS}
    src["PATH"] = "/usr/bin"
    scrubbed = _scrub_child_env(src, is_passthrough=lambda _: False, is_windows=False)
    for k in CACHE_ENV_VARS:
        assert k not in scrubbed, f"{k} should be dropped by the scrub"
    assert scrubbed.get("PATH") == "/usr/bin"  # sanity: safe var survives


def test_child_cache_redirect_env_points_under_hermes_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = _child_cache_redirect_env()
    expected_base = os.path.join(str(tmp_path), "scratch", "caches")
    # The child runs with PYTHONDONTWRITEBYTECODE=1, so __pycache__ is never
    # written — PYTHONPYCACHEPREFIX is intentionally dropped as a dead no-op.
    assert "PYTHONPYCACHEPREFIX" not in env
    assert env["MYPY_CACHE_DIR"].startswith(expected_base)
    assert os.path.isabs(env["RUFF_CACHE_DIR"])
    assert "npm_config_cache" in env and "PYTEST_ADDOPTS" in env


def test_injection_restores_cache_vars_after_scrub(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scrubbed = _scrub_child_env(
        {"MYPY_CACHE_DIR": "/work/mypy", "PATH": "/usr/bin"},
        is_passthrough=lambda _: False, is_windows=False,
    )
    assert "MYPY_CACHE_DIR" not in scrubbed  # dropped by the scrub
    for k, v in _child_cache_redirect_env().items():
        scrubbed.setdefault(k, v)             # production re-injects post-scrub
    assert scrubbed["MYPY_CACHE_DIR"].startswith(str(tmp_path))
    assert os.path.join("scratch", "caches") in scrubbed["MYPY_CACHE_DIR"]
    # PYTHONPYCACHEPREFIX is a dead no-op under PYTHONDONTWRITEBYTECODE=1 and is
    # never re-injected.
    assert "PYTHONPYCACHEPREFIX" not in scrubbed


def test_inject_sets_cache_vars_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child_env = {"PATH": "/usr/bin"}
    _inject_child_cache_redirect(child_env)
    # The redirected caches land under the scratch base when not already set.
    assert child_env["MYPY_CACHE_DIR"].startswith(str(tmp_path))
    assert "RUFF_CACHE_DIR" in child_env and "npm_config_cache" in child_env


def test_inject_does_not_clobber_explicit_passthrough_value(monkeypatch, tmp_path):
    # A cache var the user explicitly opted into terminal.env_passthrough
    # survives _scrub_child_env; the injection must NOT overwrite it (setdefault
    # semantics), matching the startup contract that an explicit override wins.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child_env = _scrub_child_env(
        {"MYPY_CACHE_DIR": "/user/explicit/mypy", "PATH": "/usr/bin"},
        is_passthrough=lambda k: k == "MYPY_CACHE_DIR",
        is_windows=False,
    )
    assert child_env["MYPY_CACHE_DIR"] == "/user/explicit/mypy"  # survived scrub
    _inject_child_cache_redirect(child_env)
    assert child_env["MYPY_CACHE_DIR"] == "/user/explicit/mypy"  # NOT clobbered
    # Vars the user did not pass through still get the scratch default.
    assert child_env["RUFF_CACHE_DIR"].startswith(str(tmp_path))


def test_sandbox_cache_env_prefix_is_shell_quoted_and_sandbox_scoped():
    s = _sandbox_cache_env_prefix("/tmp/sbx with space")
    tokens = shlex.split(s)  # must parse cleanly despite the space
    assert len(tokens) == 2
    keys = {t.split("=", 1)[0] for t in tokens}
    # PYTHONPYCACHEPREFIX is omitted: the remote script runs with
    # PYTHONDONTWRITEBYTECODE=1, so a pycache prefix would be dead.
    assert keys == {"MYPY_CACHE_DIR", "RUFF_CACHE_DIR"}
    for t in tokens:
        val = t.split("=", 1)[1]
        assert val.startswith("/tmp/sbx with space/.caches/")


def test_sandbox_cache_env_prefix_stays_posix_on_a_windows_host(monkeypatch):
    # sandbox_dir is ALWAYS a remote POSIX path (mkdir -p over env.execute). On a
    # Windows desktop driving a remote POSIX backend, building these dirs through
    # host-OS os.path would emit drive-letter/backslash paths to the remote bash,
    # reintroducing the cwd clutter this helper exists to prevent. Simulate a
    # Windows host by pointing the cache-redirect module's os.path at ntpath; the
    # emitted dirs must stay POSIX regardless.
    import ntpath
    import types

    import agent.cache_redirect as cr

    monkeypatch.setattr(cr, "os", types.SimpleNamespace(path=ntpath, environ=os.environ))

    s = _sandbox_cache_env_prefix("/tmp/sbx")
    for token in shlex.split(s):
        value = token.split("=", 1)[1]
        assert "\\" not in value, f"backslash leaked into remote POSIX path: {value}"
        assert ":" not in value, f"drive letter leaked into remote POSIX path: {value}"
        assert value.startswith("/tmp/sbx/.caches/")
