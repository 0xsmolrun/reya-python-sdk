"""Configuration for the Reya 15m FVG bot.

Configuration comes from three layers, later layers winning:

1. the dataclass defaults in this module (sane, conservative values),
2. ``config.yaml`` (strategy / risk / execution / UI tuning),
3. the process environment and ``.env`` (secrets and network endpoints only).

Secrets are never read from YAML so that ``config.yaml`` stays safe to commit.
"""

from typing import Any, Dict, List, Mapping, Optional, Type, TypeVar, Union, get_args, get_origin

import dataclasses
import os
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv

from sdk.reya_rest_api.config import MAINNET_CHAIN_ID, TradingConfig

from bot.utils.numbers import to_decimal

T = TypeVar("T")

DEFAULT_CONFIG_PATH = "config.yaml"

#: Endpoints keyed by chain id, used when the environment does not override them.
NETWORK_DEFAULTS: Dict[int, Dict[str, str]] = {
    1729: {
        "api_url": "https://api.reya.xyz/v2",
        "ws_url": "wss://ws.reya.xyz/",
        "name": "reya-mainnet",
    },
    89346162: {
        "api_url": "https://api-cronos.reya.xyz/v2",
        "ws_url": "wss://websocket-testnet.reya.xyz/",
        "name": "reya-testnet",
    },
}


class ConfigError(ValueError):
    """Raised when the bot cannot assemble a usable configuration."""


def _coerce(value: Any, annotation: Any) -> Any:
    """Coerce a YAML/env scalar into the type a dataclass field declares."""
    origin = get_origin(annotation)
    if origin is Union:  # Optional[X] and friends
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if value is None:
            return None
        return _coerce(value, args[0]) if args else value
    if origin in (list, List):
        (item_type,) = get_args(annotation) or (str,)
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        return [_coerce(item, item_type) for item in value or []]
    if annotation is Decimal:
        return to_decimal(value)
    if annotation is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if annotation is int:
        return int(value)
    if annotation is float:
        return float(value)
    if annotation is str:
        return str(value)
    return value


def _build(cls: Type[T], mapping: Optional[Mapping[str, Any]]) -> T:
    """Instantiate a dataclass from a mapping, ignoring unknown keys."""
    data = dict(mapping or {})
    known = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"Unknown {cls.__name__} option(s): {', '.join(sorted(unknown))}")
    kwargs = {name: _coerce(data[name], known[name].type) for name in data}
    return cls(**kwargs)  # type: ignore[call-arg]


@dataclass
class ExchangeConfig:
    """Network endpoints and credentials for the Reya API."""

    chain_id: int = MAINNET_CHAIN_ID
    api_url: str = NETWORK_DEFAULTS[MAINNET_CHAIN_ID]["api_url"]
    ws_url: str = NETWORK_DEFAULTS[MAINNET_CHAIN_ID]["ws_url"]
    account_id: Optional[int] = None
    private_key: Optional[str] = None
    owner_wallet_address: str = ""

    @property
    def is_mainnet(self) -> bool:
        """Whether the configured chain is Reya mainnet."""
        return self.chain_id == MAINNET_CHAIN_ID

    @property
    def network_name(self) -> str:
        """Human readable network label for the UI."""
        return NETWORK_DEFAULTS.get(self.chain_id, {}).get("name", f"chain-{self.chain_id}")

    def to_trading_config(self) -> TradingConfig:
        """Build the SDK's :class:`TradingConfig` from these values."""
        if not self.owner_wallet_address:
            raise ConfigError(
                "OWNER_WALLET_ADDRESS (or PERP_WALLET_ADDRESS_1) must be set: "
                "it is the wallet whose positions, orders and balances the bot reads."
            )
        return TradingConfig(
            api_url=self.api_url,
            chain_id=self.chain_id,
            owner_wallet_address=self.owner_wallet_address,
            private_key=self.private_key,
            account_id=self.account_id,
        )

    def require_trading_credentials(self) -> None:
        """Validate that live order entry is possible.

        Raises:
            ConfigError: If the private key or account id is missing.
        """
        missing = []
        if not self.private_key:
            missing.append("PRIVATE_KEY")
        if self.account_id is None:
            missing.append("ACCOUNT_ID")
        if missing:
            raise ConfigError(
                f"Live trading needs {' and '.join(missing)} in the environment. "
                "Run with --mode paper to trade against live prices without signing orders."
            )


@dataclass
class StrategyConfig:
    """15m Fair Value Gap detection and entry-qualification parameters."""

    #: Candle resolution passed to ``/v2/candleHistory/{symbol}/{resolution}``.
    timeframe: str = "15m"
    #: Number of historical candles to warm up with (paginated 200 at a time).
    history_bars: int = 500
    #: Wilder ATR period used for the gap-size filter and stop buffer.
    atr_period: int = 14
    #: A gap only counts if its height is at least this many ATRs.
    min_fvg_atr_mult: Decimal = Decimal("0.25")
    #: Ignore gaps smaller than this fraction of price (guards against dust gaps).
    min_fvg_price_pct: Decimal = Decimal("0.0005")
    #: Require candles i-2, i-1, i to all close in the gap's direction.
    require_three_candle_run: bool = True
    #: Require the middle (displacement) candle to have a large real body.
    require_displacement: bool = True
    #: Displacement candle body must be at least this many ATRs.
    displacement_atr_mult: Decimal = Decimal("0.6")
    #: Require a liquidity sweep of a recent swing before the gap.
    require_liquidity_sweep: bool = False
    #: Bars scanned for the swing that a sweep must take out.
    sweep_lookback: int = 12
    #: Directional filter: ``none``, ``ma`` (EMA slope + side) or ``structure``.
    bias_mode: str = "ma"
    #: EMA period for ``bias_mode: ma``.
    bias_ma_period: int = 50
    #: Swing lookback for ``bias_mode: structure``.
    structure_lookback: int = 20
    #: Where inside the gap the limit entry sits: 0 = far edge, 1 = near edge.
    #: 0.5 places the entry at the gap midpoint.
    entry_zone_ratio: Decimal = Decimal("0.5")
    #: Gaps older than this many bars stop being tradeable.
    max_fvg_age_bars: int = 60
    #: ``touch`` invalidates a gap on first tag, ``full`` waits for a full fill.
    mitigation_mode: str = "full"
    #: Maximum gaps tracked per symbol (newest kept).
    max_tracked_fvgs: int = 40
    #: Do not enter if price has already run past the far edge of the gap.
    reject_if_beyond_far_edge: bool = True

    def validate(self) -> None:
        """Check invariants that would otherwise fail deep inside the engine."""
        if self.atr_period < 2:
            raise ConfigError("strategy.atr_period must be >= 2")
        if self.history_bars < self.atr_period + 5:
            raise ConfigError("strategy.history_bars must exceed atr_period by at least 5 bars")
        if not Decimal(0) <= self.entry_zone_ratio <= Decimal(1):
            raise ConfigError("strategy.entry_zone_ratio must be between 0 and 1")
        if self.bias_mode not in ("none", "ma", "structure"):
            raise ConfigError("strategy.bias_mode must be one of: none, ma, structure")
        if self.mitigation_mode not in ("touch", "full"):
            raise ConfigError("strategy.mitigation_mode must be one of: touch, full")


@dataclass
class RiskConfig:
    """Position sizing and the circuit breakers that stop the bot trading."""

    #: Percentage of account equity risked per trade (0.5 = 0.5%).
    risk_per_trade_pct: Decimal = Decimal("0.5")
    #: Hard ceiling on the above, applied after any runtime adjustment.
    max_risk_per_trade_pct: Decimal = Decimal("2.0")
    #: Stop is placed this many ATRs beyond the far edge of the gap.
    sl_atr_buffer_mult: Decimal = Decimal("0.5")
    #: Floor on stop distance, as a multiple of ATR, to survive normal noise.
    min_stop_atr_mult: Decimal = Decimal("0.5")
    #: ``rr`` for a fixed reward multiple, ``opposing_fvg`` to target the next
    #: opposing gap (falling back to ``rr`` when there is none).
    tp_mode: str = "rr"
    #: Reward-to-risk multiple for ``tp_mode: rr``.
    risk_reward: Decimal = Decimal("2.0")
    #: Signals offering less than this R:R are skipped.
    min_risk_reward: Decimal = Decimal("1.5")
    #: Maximum simultaneous open positions across all symbols.
    max_open_positions: int = 2
    #: Trading halts for the rest of the day past this equity drawdown.
    max_daily_loss_pct: Decimal = Decimal("3.0")
    #: Maximum entries per UTC day.
    max_daily_trades: int = 10
    #: Consecutive losses that trigger the cooldown.
    consecutive_loss_limit: int = 3
    #: Cooldown length in minutes once the loss streak trips.
    cooldown_minutes: int = 120
    #: Notional cap per position as a percentage of equity.
    max_notional_pct: Decimal = Decimal("300")
    #: Ignore equity above this figure when sizing (0 disables the cap).
    equity_cap: Decimal = Decimal("0")

    def validate(self) -> None:
        """Check invariants that would otherwise fail deep inside the engine."""
        if self.risk_per_trade_pct <= 0:
            raise ConfigError("risk.risk_per_trade_pct must be positive")
        if self.risk_per_trade_pct > self.max_risk_per_trade_pct:
            raise ConfigError("risk.risk_per_trade_pct cannot exceed risk.max_risk_per_trade_pct")
        if self.tp_mode not in ("rr", "opposing_fvg"):
            raise ConfigError("risk.tp_mode must be one of: rr, opposing_fvg")
        if self.risk_reward <= 0:
            raise ConfigError("risk.risk_reward must be positive")
        if self.max_open_positions < 1:
            raise ConfigError("risk.max_open_positions must be >= 1")


@dataclass
class ExecutionConfig:
    """Order placement, monitoring and shutdown behaviour."""

    #: How far through the book the IOC entry limit is priced, in basis points.
    #: This is what makes the limit marketable; it is a slippage cap, not a fee.
    entry_slippage_bps: Decimal = Decimal("15")
    #: Same, for exits (reduce-only closes need to fill).
    exit_slippage_bps: Decimal = Decimal("30")
    #: Seconds an IOC order is valid for.
    entry_expiry_s: int = 10
    #: Attach SL and TP trigger orders immediately after an entry fills.
    attach_brackets: bool = True
    #: Seconds between REST reconciliations of positions and orders.
    reconcile_interval_s: int = 20
    #: Seconds between candle polls. 15m candles need no faster cadence.
    candle_poll_interval_s: int = 20
    #: Seconds to wait for an entry fill to appear before giving up on it.
    fill_timeout_s: int = 15
    #: Cancel every resting order for traded symbols on shutdown.
    cancel_orders_on_shutdown: bool = True
    #: Also flatten open positions on shutdown (off by default: brackets remain).
    close_positions_on_shutdown: bool = False
    #: Refuse to place a new entry within this many seconds of the previous one.
    min_seconds_between_entries: int = 30
    #: Log orders instead of sending them, even in live mode.
    dry_run: bool = False

    def validate(self) -> None:
        """Check invariants that would otherwise fail deep inside the engine."""
        if self.entry_slippage_bps < 0 or self.exit_slippage_bps < 0:
            raise ConfigError("execution slippage must be non-negative")
        if self.entry_expiry_s < 1:
            raise ConfigError("execution.entry_expiry_s must be >= 1")


@dataclass
class PaperConfig:
    """Fill simulation parameters for paper trading."""

    #: Starting equity for the simulated account.
    starting_equity: Decimal = Decimal("10000")
    #: Taker fee applied to both sides of a simulated trade, in basis points.
    fee_bps: Decimal = Decimal("2")
    #: Slippage applied to simulated fills, in basis points.
    slippage_bps: Decimal = Decimal("2")


@dataclass
class NotifierConfig:
    """Optional Telegram / Discord alerting."""

    enabled: bool = False
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    #: Which event kinds are forwarded.
    notify_entries: bool = True
    notify_exits: bool = True
    notify_errors: bool = True
    notify_halts: bool = True

    @property
    def has_target(self) -> bool:
        """Whether at least one delivery channel is fully configured."""
        telegram = bool(self.telegram_bot_token and self.telegram_chat_id)
        return telegram or bool(self.discord_webhook_url)


@dataclass
class UIConfig:
    """Matrix terminal UI settings."""

    #: Frame interval for data panels, in milliseconds.
    refresh_ms: int = 400
    #: Frame interval for the digital rain, in milliseconds.
    rain_ms: int = 90
    #: Rain enabled at all (disable on very slow terminals).
    rain_enabled: bool = True
    #: Fraction of columns that carry a rain stream at rest, 0..1.
    rain_density: float = 0.6
    #: Rows of strategy log kept on screen.
    log_lines: int = 200
    #: Depth levels rendered per side.
    depth_levels: int = 8
    #: Recent market trades rendered.
    trade_rows: int = 12


@dataclass
class BotConfig:
    """Top-level bot configuration."""

    #: ``live``, ``paper`` or ``backtest``.
    mode: str = "paper"
    #: Symbols the bot trades. The first is the UI's initial focus.
    symbols: List[str] = field(default_factory=lambda: ["ETHRUSDPERP"])
    log_level: str = "INFO"
    log_file: Optional[str] = "logs/reya_fvg_bot.log"
    #: Start the strategy immediately, or wait for the operator to press ``s``.
    autostart: bool = False
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    @property
    def primary_symbol(self) -> str:
        """The symbol the UI focuses on at startup."""
        return self.symbols[0]

    @property
    def is_paper(self) -> bool:
        """Whether fills are simulated rather than sent to the exchange."""
        return self.mode == "paper"

    def validate(self) -> None:
        """Validate the whole tree, raising :class:`ConfigError` on any problem."""
        if self.mode not in ("live", "paper", "backtest"):
            raise ConfigError("mode must be one of: live, paper, backtest")
        if not self.symbols:
            raise ConfigError("at least one symbol must be configured")
        duplicates = {s for s in self.symbols if self.symbols.count(s) > 1}
        if duplicates:
            raise ConfigError(f"duplicate symbols configured: {', '.join(sorted(duplicates))}")
        self.strategy.validate()
        self.risk.validate()
        self.execution.validate()
        if self.mode == "live":
            self.exchange.require_trading_credentials()

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the configuration, with secrets redacted."""

        def _plain(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {k: _plain(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_plain(v) for v in value]
            return value

        data = _plain(dataclasses.asdict(self))
        secrets = data.get("exchange", {})
        if secrets.get("private_key"):
            secrets["private_key"] = "***redacted***"
        notifier = data.get("notifier", {})
        for key in ("telegram_bot_token", "discord_webhook_url"):
            if notifier.get(key):
                notifier[key] = "***redacted***"
        return data


def load_yaml_config(path: Optional[str]) -> Dict[str, Any]:
    """Read a YAML config file, returning ``{}`` when it does not exist.

    Args:
        path: Path to the YAML file. ``None`` uses :data:`DEFAULT_CONFIG_PATH`
            and tolerates its absence.

    Raises:
        ConfigError: If an explicitly requested file is missing or malformed.
    """
    explicit = path is not None
    target = Path(path or DEFAULT_CONFIG_PATH)
    if not target.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {target}")
        return {}
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {target}: {exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"{target} must contain a YAML mapping at the top level")
    return data


def _env_first(*names: str) -> Optional[str]:
    """Return the first environment variable that is set and non-empty."""
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return None


def load_exchange_config(overrides: Optional[Mapping[str, Any]] = None) -> ExchangeConfig:
    """Build the exchange configuration from the environment.

    The variable names from the bot's own ``.env.example`` take precedence, and
    the SDK's ``PERP_*`` names are accepted as a fallback so an existing SDK
    ``.env`` keeps working.
    """
    chain_raw = _env_first("CHAIN_ID")
    chain_id = int(chain_raw) if chain_raw else MAINNET_CHAIN_ID
    defaults = NETWORK_DEFAULTS.get(chain_id, NETWORK_DEFAULTS[MAINNET_CHAIN_ID])

    account_raw = _env_first("ACCOUNT_ID", "PERP_ACCOUNT_ID_1")
    config = ExchangeConfig(
        chain_id=chain_id,
        api_url=_env_first("REYA_API_BASE_URL", "REYA_API_URL") or defaults["api_url"],
        ws_url=_env_first("REYA_WS_URL") or defaults["ws_url"],
        account_id=int(account_raw) if account_raw else None,
        private_key=_env_first("PRIVATE_KEY", "PERP_PRIVATE_KEY_1"),
        owner_wallet_address=_env_first("OWNER_WALLET_ADDRESS", "PERP_WALLET_ADDRESS_1") or "",
    )
    for key, value in (overrides or {}).items():
        if value is not None:
            setattr(config, key, value)
    return config


def load_config(
    config_path: Optional[str] = None,
    *,
    mode: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    log_level: Optional[str] = None,
    dry_run: Optional[bool] = None,
    load_env: bool = True,
) -> BotConfig:
    """Assemble the full :class:`BotConfig` from YAML, ``.env`` and CLI overrides.

    Args:
        config_path: Optional path to ``config.yaml``.
        mode: CLI override for the run mode.
        symbols: CLI override for the traded symbols.
        log_level: CLI override for the log level.
        dry_run: CLI override that suppresses order submission.
        load_env: Whether to load a ``.env`` file into the environment.

    Returns:
        A validated configuration.

    Raises:
        ConfigError: If any layer contains an unknown or invalid option.
    """
    if load_env:
        load_dotenv()

    raw = load_yaml_config(config_path)
    unknown_sections = set(raw) - {f.name for f in fields(BotConfig)}
    if unknown_sections:
        raise ConfigError(f"Unknown config section(s): {', '.join(sorted(unknown_sections))}")

    config = BotConfig(
        mode=str(raw.get("mode", BotConfig.mode)),
        symbols=[str(s).upper() for s in raw.get("symbols", ["ETHRUSDPERP"])],
        log_level=str(raw.get("log_level", BotConfig.log_level)),
        log_file=raw.get("log_file", BotConfig.log_file),
        autostart=bool(raw.get("autostart", BotConfig.autostart)),
        exchange=load_exchange_config(raw.get("exchange")),
        strategy=_build(StrategyConfig, raw.get("strategy")),
        risk=_build(RiskConfig, raw.get("risk")),
        execution=_build(ExecutionConfig, raw.get("execution")),
        paper=_build(PaperConfig, raw.get("paper")),
        notifier=_build(NotifierConfig, raw.get("notifier")),
        ui=_build(UIConfig, raw.get("ui")),
    )

    # Notifier secrets only ever come from the environment.
    config.notifier.telegram_bot_token = _env_first("TELEGRAM_BOT_TOKEN") or config.notifier.telegram_bot_token
    config.notifier.telegram_chat_id = _env_first("TELEGRAM_CHAT_ID") or config.notifier.telegram_chat_id
    config.notifier.discord_webhook_url = _env_first("DISCORD_WEBHOOK_URL") or config.notifier.discord_webhook_url
    if config.notifier.enabled and not config.notifier.has_target:
        config.notifier.enabled = False

    if mode is not None:
        config.mode = mode
    if symbols:
        config.symbols = [s.upper() for s in symbols]
    if log_level is not None:
        config.log_level = log_level
    if dry_run is not None:
        config.execution.dry_run = dry_run

    config.validate()
    return config


__all__ = [
    "BotConfig",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "ExchangeConfig",
    "ExecutionConfig",
    "NETWORK_DEFAULTS",
    "NotifierConfig",
    "PaperConfig",
    "RiskConfig",
    "StrategyConfig",
    "UIConfig",
    "load_config",
    "load_exchange_config",
    "load_yaml_config",
]
