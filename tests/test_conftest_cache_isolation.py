"""Guards the suite-wide cache-redirect env isolation in conftest.

hermes_cli.main mutates the cache-redirect vars (PYTEST_ADDOPTS,
PYTHONPYCACHEPREFIX, ...) via raw ``os.environ.setdefault`` at import time —
NOT via monkeypatch — so without an explicit snapshot/restore in conftest a
test that imports/re-imports hermes_cli.main would leak a redirected cache_dir
into later tests in the same file (and any pytest subprocess they spawn).

These two ordered tests simulate that leak: the first writes a raw value, the
second asserts the autouse conftest fixture cleaned it up.
"""
import os

from agent.cache_redirect import CACHE_ENV_VARS

_SENTINEL = "leaked-by-conftest-isolation-test"


def test_a_simulates_a_raw_env_leak():
    # Mimic apply_cache_redirect_from_hermes_home(): a raw os.environ write that
    # monkeypatch does not track and would otherwise persist to the next test.
    for var in CACHE_ENV_VARS:
        os.environ[var] = _SENTINEL
    assert os.environ["PYTEST_ADDOPTS"] == _SENTINEL


def test_b_does_not_see_the_leak():
    # The autouse conftest fixture must have restored these around test_a, so the
    # sentinel cannot survive into this test.
    for var in CACHE_ENV_VARS:
        assert os.environ.get(var) != _SENTINEL, f"{var} leaked across tests"
