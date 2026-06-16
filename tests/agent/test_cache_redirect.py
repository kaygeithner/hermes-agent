import os

from agent.cache_redirect import (
    CACHE_ENV_VARS,
    apply_cache_redirect_defaults,
    cache_redirect_env,
)

# apply_cache_redirect_defaults setdefaults these into the REAL os.environ; the
# autouse ``_isolate_cache_redirect_env`` fixture in tests/conftest.py snapshots
# and restores the whole CACHE_ENV_VARS set around every test, so nothing here
# leaks into the shared session (or a pytest subprocess a later test spawns).


def test_cache_env_vars_matches_producer_keys(tmp_path):
    # CACHE_ENV_VARS must enumerate exactly the keys cache_redirect_env emits, so
    # the test-isolation fixture and code-exec injection can rely on it as the
    # single source of truth.
    assert set(CACHE_ENV_VARS) == set(cache_redirect_env(str(tmp_path)))


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

def test_cache_redirect_env_omits_pycache_when_bytecode_disabled(tmp_path):
    # Bytecode-disabled callers (the code-exec child / remote sandbox run with
    # PYTHONDONTWRITEBYTECODE=1, so __pycache__ is never written) get a dead
    # PYTHONPYCACHEPREFIX. The producer owns that omission via include_pycache so
    # each such caller doesn't have to remember to pop the key itself.
    env = cache_redirect_env(str(tmp_path), include_pycache=False)
    assert "PYTHONPYCACHEPREFIX" not in env
    # Every other relocation still comes through.
    assert env["MYPY_CACHE_DIR"].startswith(str(tmp_path))
    assert {"MYPY_CACHE_DIR", "RUFF_CACHE_DIR", "PYTEST_ADDOPTS", "npm_config_cache"} <= set(env)
    # The default still includes it (the service process may write bytecode).
    assert "PYTHONPYCACHEPREFIX" in cache_redirect_env(str(tmp_path))


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


def test_pytest_addopts_relocates_cache_dir_rather_than_disabling(tmp_path):
    # pytest cache must be RELOCATED (so --lf/--ff/--sw keep working), not
    # disabled via -p no:cacheprovider.
    import shlex

    opts = cache_redirect_env(str(tmp_path))["PYTEST_ADDOPTS"]
    assert "no:cacheprovider" not in opts          # not disabled
    parts = shlex.split(opts)                       # pytest parses via shlex
    assert parts[0] == "-o"
    assert parts[1].startswith("cache_dir=")
    assert str(tmp_path) in parts[1]                # relocated under the base


def test_apply_defaults_is_best_effort_and_never_raises(tmp_path, monkeypatch):
    # A cache-tidiness step must NEVER crash startup. Point base_dir under a
    # path whose parent is a regular file -> os.makedirs raises
    # NotADirectoryError; apply must swallow it and degrade (env vars NOT set),
    # exactly as it would on a read-only / disk-full / unwritable HERMES_HOME.
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x", encoding="utf-8")
    bad_base = str(blocker / "scratch" / "caches")  # parent is a file
    for var in ("PYTHONPYCACHEPREFIX", "MYPY_CACHE_DIR", "RUFF_CACHE_DIR",
                "PYTEST_ADDOPTS", "npm_config_cache"):
        monkeypatch.delenv(var, raising=False)

    apply_cache_redirect_defaults(bad_base)  # must NOT raise

    assert "PYTHONPYCACHEPREFIX" not in os.environ  # degraded to prior behavior
