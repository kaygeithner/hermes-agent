"""Per-channel model override display: /status + /reset banner.

Regression test for the display gap where /status and the /reset banner
reported the global default model on a fresh session even when the channel is
pinned via ``discord.channel_overrides`` (the actual runs already honoured the
pin). Both display paths should now resolve the channel override before
falling back to the global default.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.session import SessionSource
from gateway.run import (
    GatewayRunner,
    _channel_override_for_source,
)


def _cfg(overrides: dict) -> GatewayConfig:
    return GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                channel_overrides=overrides,
            )
        }
    )


def _source(thread_id: str, parent_id: str = "") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=thread_id,
        chat_type="thread",
        thread_id=thread_id,
        parent_chat_id=parent_id or thread_id,
    )


class TestChannelOverrideForSource:
    def test_exact_thread_id_wins(self):
        cfg = _cfg({
            "111": ChannelOverride(model="deepseek-v4-flash-0731", provider="custom:spark-flash"),
            "111:222": ChannelOverride(model="parent-model", provider="anthropic"),
        })
        ov = _channel_override_for_source(
            cfg, _source(thread_id="111", parent_id="999")
        )
        assert ov is not None
        assert ov.model == "deepseek-v4-flash-0731"
        assert ov.provider == "custom:spark-flash"

    def test_thread_without_own_override_falls_back_to_parent(self):
        cfg = _cfg({
            "999": ChannelOverride(model="parent-model", provider="anthropic"),
        })
        ov = _channel_override_for_source(
            cfg, _source(thread_id="111", parent_id="999")
        )
        assert ov is not None
        assert ov.model == "parent-model"

    def test_no_match_returns_none(self):
        cfg = _cfg({})
        assert (
            _channel_override_for_source(
                cfg, _source(thread_id="111", parent_id="999")
            )
            is None
        )
        assert _channel_override_for_source(cfg, None) is None
        assert _channel_override_for_source(None, _source("111")) is None


class _FakeResolved:
    model = "grok-4.6"
    provider = "xai-oauth"
    base_url = ""
    context_length = 500_000
    context_source = "config"


class TestResetBannerAdvertisesChannelOverride:
    def test_banner_shows_pinned_model_for_overridden_channel(
        self, monkeypatch
    ):
        from gateway import run as run_mod

        monkeypatch.setattr(
            run_mod, "_resolve_gateway_model_context", lambda: _FakeResolved()
        )
        cfg = _cfg({
            "111": ChannelOverride(
                model="deepseek-v4-flash-0731", provider="custom:spark-flash"
            ),
        })
        runner = MagicMock()
        runner.config = cfg  # type: ignore[attr-defined]

        info = run_mod.GatewayRunner._format_session_info(
            runner, _source(thread_id="111")
        )

        assert "◆ Model: `deepseek-v4-flash-0731`" in info
        assert "◆ Provider: custom:spark-flash" in info

    def test_banner_uses_global_default_without_override(
        self, monkeypatch
    ):
        from gateway import run as run_mod

        monkeypatch.setattr(
            run_mod, "_resolve_gateway_model_context", lambda: _FakeResolved()
        )
        cfg = _cfg({})
        runner = MagicMock()
        runner.config = cfg  # type: ignore[attr-defined]

        info = run_mod.GatewayRunner._format_session_info(runner, None)

        assert "◆ Model: `grok-4.6`" in info
        assert "◆ Provider: xai-oauth" in info


class TestStatusCommandAdvertisesChannelOverride:
    @pytest.mark.asyncio
    async def test_status_shows_pinned_model_on_fresh_session(self, monkeypatch):
        from gateway import run as run_mod
        from gateway.slash_commands import GatewaySlashCommandsMixin

        # Fresh-session look: no persisted route, no live agent, empty row.
        session_entry = MagicMock()
        session_entry.session_key = "agent:main:discord:thread:111:111"
        session_entry.session_id = "20260820_051120_37c1cbf1"
        session_entry.created_at = __import__("datetime").datetime(2026, 8, 20, 5, 11)
        session_entry.updated_at = __import__("datetime").datetime(2026, 8, 20, 5, 11)
        session_entry.last_prompt_tokens = 0

        handler = MagicMock()
        handler.config = _cfg({
            "111": ChannelOverride(
                model="deepseek-v4-flash-0731", provider="custom:spark-flash"
            ),
        })
        handler.async_session_store = MagicMock()
        handler.async_session_store.get_or_create_session = AsyncMock(
            return_value=session_entry
        )
        handler._running_agents = {}
        handler._agent_cache = {}
        handler._agent_cache_lock = None
        handler._session_db = MagicMock()
        handler._session_db.get_session_title = AsyncMock(return_value=None)
        handler._session_db.get_session = AsyncMock(return_value={})
        handler._session_db.get_dominant_session_model_route = AsyncMock(
            return_value={}
        )
        handler.adapters = {}

        # Force the config-default fallback values so only the channel
        # override can supply the model.
        monkeypatch.setattr(
            run_mod, "_load_gateway_config",
            lambda: {"model": {"default": "grok-4.6", "provider": "xai-oauth", "context_length": 500000}},
        )
        monkeypatch.setattr(
            run_mod, "_resolve_gateway_model", lambda user_config: "grok-4.6"
        )

        event = MagicMock()
        event.source = _source(thread_id="111")

        text = await GatewaySlashCommandsMixin._handle_status_command(
            handler, event
        )

        assert "deepseek-v4-flash-0731" in text
        assert "grok-4.6" not in text
