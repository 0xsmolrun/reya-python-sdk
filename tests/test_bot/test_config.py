"""Tests for configuration loading, layering and validation."""

from decimal import Decimal

import pytest

from bot.config import (
    BotConfig,
    ConfigError,
    ExchangeConfig,
    RiskConfig,
    StrategyConfig,
    load_config,
    load_exchange_config,
    load_yaml_config,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from an environment with no Reya variables set."""
    for name in (
        "CHAIN_ID",
        "ACCOUNT_ID",
        "PRIVATE_KEY",
        "OWNER_WALLET_ADDRESS",
        "REYA_API_BASE_URL",
        "REYA_API_URL",
        "REYA_WS_URL",
        "PERP_ACCOUNT_ID_1",
        "PERP_PRIVATE_KEY_1",
        "PERP_WALLET_ADDRESS_1",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class TestExchangeConfig:
    def test_defaults_to_mainnet(self):
        config = load_exchange_config()
        assert config.chain_id == 1729
        assert config.api_url == "https://api.reya.xyz/v2"
        assert config.ws_url == "wss://ws.reya.xyz/"
        assert config.is_mainnet

    def test_testnet_chain_selects_testnet_endpoints(self, monkeypatch):
        monkeypatch.setenv("CHAIN_ID", "89346162")
        config = load_exchange_config()

        assert not config.is_mainnet
        assert config.api_url == "https://api-cronos.reya.xyz/v2"
        assert "testnet" in config.ws_url
        assert config.network_name == "reya-testnet"

    def test_reads_the_bot_variable_names(self, monkeypatch):
        monkeypatch.setenv("ACCOUNT_ID", "42")
        monkeypatch.setenv("PRIVATE_KEY", "0xdeadbeef")
        monkeypatch.setenv("OWNER_WALLET_ADDRESS", "0xabc")
        config = load_exchange_config()

        assert config.account_id == 42
        assert config.private_key == "0xdeadbeef"
        assert config.owner_wallet_address == "0xabc"

    def test_falls_back_to_the_sdk_variable_names(self, monkeypatch):
        monkeypatch.setenv("PERP_ACCOUNT_ID_1", "7")
        monkeypatch.setenv("PERP_PRIVATE_KEY_1", "0xkey")
        monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0xwallet")
        config = load_exchange_config()

        assert config.account_id == 7
        assert config.private_key == "0xkey"
        assert config.owner_wallet_address == "0xwallet"

    def test_bot_names_win_over_sdk_names(self, monkeypatch):
        monkeypatch.setenv("ACCOUNT_ID", "1")
        monkeypatch.setenv("PERP_ACCOUNT_ID_1", "2")
        assert load_exchange_config().account_id == 1

    def test_api_url_override(self, monkeypatch):
        monkeypatch.setenv("REYA_API_BASE_URL", "https://example.test/v2")
        assert load_exchange_config().api_url == "https://example.test/v2"

    def test_trading_config_requires_an_owner_wallet(self):
        with pytest.raises(ConfigError, match="OWNER_WALLET_ADDRESS"):
            ExchangeConfig().to_trading_config()

    def test_trading_config_is_built_for_the_sdk(self):
        config = ExchangeConfig(
            chain_id=1729,
            account_id=5,
            private_key="0xkey",
            owner_wallet_address="0xwallet",
        )
        trading = config.to_trading_config()

        assert trading.account_id == 5
        assert trading.owner_wallet_address == "0xwallet"
        assert trading.chain_id == 1729

    def test_live_trading_requires_credentials(self):
        with pytest.raises(ConfigError, match="PRIVATE_KEY and ACCOUNT_ID"):
            ExchangeConfig(owner_wallet_address="0xw").require_trading_credentials()

    def test_live_trading_accepts_full_credentials(self):
        ExchangeConfig(
            owner_wallet_address="0xw",
            private_key="0xkey",
            account_id=1,
        ).require_trading_credentials()


class TestYamlLayering:
    def test_missing_default_file_is_fine(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert load_yaml_config(None) == {}

    def test_missing_explicit_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_yaml_config("definitely-not-here.yaml")

    def test_malformed_yaml_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("mode: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError, match="Could not parse"):
            load_yaml_config(str(path))

    def test_non_mapping_yaml_raises(self, tmp_path):
        path = tmp_path / "list.yaml"
        path.write_text("- one\n- two\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_yaml_config(str(path))

    def test_yaml_values_override_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OWNER_WALLET_ADDRESS", "0xw")
        path = tmp_path / "config.yaml"
        path.write_text(
            "mode: paper\n"
            "symbols: [BTCRUSDPERP]\n"
            "strategy:\n"
            "  min_fvg_atr_mult: 0.75\n"
            "  bias_mode: structure\n"
            "risk:\n"
            "  risk_per_trade_pct: 0.25\n",
            encoding="utf-8",
        )
        config = load_config(str(path), load_env=False)

        assert config.symbols == ["BTCRUSDPERP"]
        assert config.strategy.min_fvg_atr_mult == Decimal("0.75")
        assert config.strategy.bias_mode == "structure"
        assert config.risk.risk_per_trade_pct == Decimal("0.25")
        # Untouched values keep their defaults.
        assert config.strategy.atr_period == 14

    def test_numeric_yaml_values_become_decimals(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("risk:\n  risk_reward: 2.5\n", encoding="utf-8")
        config = load_config(str(path), load_env=False)

        assert isinstance(config.risk.risk_reward, Decimal)
        # Parsed via str(), so the value is exactly 2.5 rather than a float blur.
        assert config.risk.risk_reward == Decimal("2.5")

    def test_unknown_section_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("nonsense:\n  a: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="Unknown config section"):
            load_config(str(path), load_env=False)

    def test_unknown_option_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("risk:\n  typo_here: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="typo_here"):
            load_config(str(path), load_env=False)

    def test_cli_overrides_beat_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("mode: paper\nsymbols: [ETHRUSDPERP]\n", encoding="utf-8")
        config = load_config(
            str(path),
            mode="backtest",
            symbols=["btcrusdperp"],
            log_level="DEBUG",
            dry_run=True,
            load_env=False,
        )

        assert config.mode == "backtest"
        assert config.symbols == ["BTCRUSDPERP"]
        assert config.log_level == "DEBUG"
        assert config.execution.dry_run is True


class TestValidation:
    def test_valid_config_passes(self):
        BotConfig(mode="paper", symbols=["ETHRUSDPERP"]).validate()

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ConfigError, match="mode must be"):
            BotConfig(mode="wat").validate()

    def test_empty_symbol_list_is_rejected(self):
        with pytest.raises(ConfigError, match="at least one symbol"):
            BotConfig(symbols=[]).validate()

    def test_duplicate_symbols_are_rejected(self):
        with pytest.raises(ConfigError, match="duplicate symbols"):
            BotConfig(symbols=["ETHRUSDPERP", "ETHRUSDPERP"]).validate()

    def test_live_mode_without_credentials_is_rejected(self):
        config = BotConfig(mode="live", symbols=["ETHRUSDPERP"])
        config.exchange = ExchangeConfig(owner_wallet_address="0xw")
        with pytest.raises(ConfigError, match="Live trading needs"):
            config.validate()

    def test_paper_mode_needs_no_credentials(self):
        config = BotConfig(mode="paper", symbols=["ETHRUSDPERP"])
        config.exchange = ExchangeConfig()
        config.validate()

    def test_entry_zone_ratio_bounds(self):
        with pytest.raises(ConfigError, match="entry_zone_ratio"):
            StrategyConfig(entry_zone_ratio=Decimal("1.5")).validate()

    def test_bias_mode_is_checked(self):
        with pytest.raises(ConfigError, match="bias_mode"):
            StrategyConfig(bias_mode="vibes").validate()

    def test_mitigation_mode_is_checked(self):
        with pytest.raises(ConfigError, match="mitigation_mode"):
            StrategyConfig(mitigation_mode="sometimes").validate()

    def test_history_must_exceed_the_atr_period(self):
        with pytest.raises(ConfigError, match="history_bars"):
            StrategyConfig(atr_period=14, history_bars=15).validate()

    def test_risk_above_the_ceiling_is_rejected(self):
        with pytest.raises(ConfigError, match="cannot exceed"):
            RiskConfig(risk_per_trade_pct=Decimal("5"), max_risk_per_trade_pct=Decimal("2")).validate()

    def test_negative_risk_is_rejected(self):
        with pytest.raises(ConfigError, match="must be positive"):
            RiskConfig(risk_per_trade_pct=Decimal("-1")).validate()

    def test_tp_mode_is_checked(self):
        with pytest.raises(ConfigError, match="tp_mode"):
            RiskConfig(tp_mode="hope").validate()


class TestSerialisation:
    def test_secrets_are_redacted(self):
        config = BotConfig()
        config.exchange = ExchangeConfig(private_key="0xsupersecret", owner_wallet_address="0xw")
        config.notifier.telegram_bot_token = "token"
        config.notifier.discord_webhook_url = "https://hook"

        dumped = config.to_dict()

        assert dumped["exchange"]["private_key"] == "***redacted***"
        assert dumped["notifier"]["telegram_bot_token"] == "***redacted***"
        assert dumped["notifier"]["discord_webhook_url"] == "***redacted***"
        assert "0xsupersecret" not in str(dumped)

    def test_decimals_render_as_strings(self):
        dumped = BotConfig().to_dict()
        assert dumped["risk"]["risk_per_trade_pct"] == "0.5"

    def test_shipped_config_file_loads(self):
        """The committed config.yaml must always be valid."""
        config = load_config("config.yaml", load_env=False)
        assert config.mode in ("live", "paper", "backtest")
        assert config.symbols


class TestNotifierConfig:
    def test_disabled_when_no_channel_is_configured(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("notifier:\n  enabled: true\n", encoding="utf-8")
        config = load_config(str(path), load_env=False)

        assert config.notifier.enabled is False

    def test_enabled_with_a_discord_webhook(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://hook.test/abc")
        path = tmp_path / "config.yaml"
        path.write_text("notifier:\n  enabled: true\n", encoding="utf-8")
        config = load_config(str(path), load_env=False)

        assert config.notifier.enabled is True
        assert config.notifier.has_target is True

    def test_telegram_needs_both_token_and_chat(self, monkeypatch):
        from bot.config import NotifierConfig

        assert NotifierConfig(telegram_bot_token="t").has_target is False
        assert NotifierConfig(telegram_bot_token="t", telegram_chat_id="c").has_target is True
