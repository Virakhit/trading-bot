from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", allow_inf_nan=False)
    mode: Literal["paper"] = "paper"
    execution_backend: Literal["local-paper", "mock-broker", "webull-th-test"] = "local-paper"
    automated_webull_test_enabled: bool = False
    session_timezone: str = "Asia/Bangkok"
    allowed_session_start: str = "09:30"
    allowed_session_end: str = "16:00"
    max_stale_seconds: int = Field(300, ge=0)
    max_order_notional: float = Field(2500, gt=0)
    max_portfolio_exposure: float = Field(10000, gt=0)
    max_orders_per_symbol_session: int = Field(20, gt=0)
    max_drawdown: float = Field(0.25, gt=0, lt=1)
    reserve_pending_orders: bool = True
    reconciliation_interval_seconds: int = Field(300, ge=1)
    scheduler_interval_seconds: int = Field(1, ge=0)
    database_url: str = "sqlite:///data/research.db"
    initial_cash: float = Field(10000, gt=0, allow_inf_nan=False)
    slippage_bps: float = Field(2, ge=0, le=100, allow_inf_nan=False)
    fee_per_share: float = Field(0.005, ge=0, allow_inf_nan=False)
    max_position_size: int = Field(100, gt=0)
    max_position_pct: float = Field(0.25, gt=0, le=1)
    max_symbol_exposure: float = Field(2500, gt=0)
    max_daily_loss: float = Field(200, gt=0)
    max_trades_per_day: int = Field(20, gt=0)
    max_concurrent_positions: int = Field(5, gt=0)
    stop_loss_pct: float | None = Field(0.03, gt=0, lt=1)
    take_profit_pct: float | None = Field(0.05, gt=0)
    loss_streak_limit: int = Field(3, gt=0)
    cooldown_seconds: int = Field(300, ge=0)
    kill_switch: bool = False
