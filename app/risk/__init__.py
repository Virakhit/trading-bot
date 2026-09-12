from dataclasses import dataclass
from app.config import Settings
from app.core.types import Bar, Signal
from app.portfolio import Portfolio, Position


@dataclass(frozen=True)
class RiskDecision:
    status: str
    codes: tuple[str, ...]


class RiskEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def exit_reason(self, position: Position, bar: Bar) -> str | None:
        if not position.quantity:
            return None
        change = bar.bid / position.average_entry - 1
        if self.settings.stop_loss_pct is not None and change <= -self.settings.stop_loss_pct:
            return "STOP_TRIGGERED"
        if self.settings.take_profit_pct is not None and change >= self.settings.take_profit_pct:
            return "TAKE_PROFIT_TRIGGERED"
        return None

    def evaluate(self, signal: Signal, quantity: int, bar: Bar, portfolio: Portfolio, *, observed_at=None) -> RiskDecision:
        s = self.settings
        codes: list[str] = []
        position = portfolio.positions.get(signal.symbol, Position())
        if signal.symbol != bar.symbol or signal.timestamp != bar.timestamp:
            codes.append("CONTEXT_MISMATCH")
        age = max(0, ((observed_at or signal.timestamp) - bar.timestamp).total_seconds())
        if age > s.max_stale_seconds:
            codes.append("STALE_DATA")
        if quantity <= 0:
            codes.append("INVALID_QUANTITY")
        if signal.action == "HOLD":
            codes.append("NO_ACTION")
        elif signal.action in ("EXIT", "SELL"):
            if quantity > position.quantity or not position.quantity:
                codes.append("NO_POSITION")
        else:
            # Kill switch blocks new exposure; risk-reducing exits remain possible.
            if s.kill_switch:
                codes.append("KILL_SWITCH")
            price = bar.ask * (1 + s.slippage_bps / 10000)
            exposure = (position.quantity + quantity) * price
            if quantity * price > s.max_order_notional:
                codes.append("MAX_ORDER_NOTIONAL")
            current_exposure = sum(p.quantity * p.mark for p in portfolio.positions.values())
            if current_exposure + quantity * price > s.max_portfolio_exposure:
                codes.append("MAX_PORTFOLIO_EXPOSURE")
            if portfolio.orders_by_symbol.get(signal.symbol, 0) >= s.max_orders_per_symbol_session:
                codes.append("MAX_ORDERS_PER_SYMBOL_SESSION")
            if portfolio.peak_equity and portfolio.equity <= portfolio.peak_equity * (1 - s.max_drawdown):
                codes.append("MAX_DRAWDOWN_KILL_SWITCH")
            if position.quantity + quantity > s.max_position_size:
                codes.append("MAX_POSITION_SIZE")
            if exposure > portfolio.equity * s.max_position_pct:
                codes.append("MAX_POSITION_PCT")
            if exposure > s.max_symbol_exposure:
                codes.append("MAX_SYMBOL_EXPOSURE")
            if quantity * (price + s.fee_per_share) > portfolio.cash:
                codes.append("INSUFFICIENT_CASH")
            if portfolio.equity <= (portfolio.day_start_equity or portfolio.equity) - s.max_daily_loss:
                codes.append("MAX_DAILY_LOSS")
            if portfolio.trades_today >= s.max_trades_per_day:
                codes.append("MAX_TRADES_PER_DAY")
            if not position.quantity and sum(p.quantity > 0 for p in portfolio.positions.values()) >= s.max_concurrent_positions:
                codes.append("MAX_CONCURRENT_POSITIONS")
            if portfolio.cooldown_until and signal.timestamp < portfolio.cooldown_until:
                codes.append("LOSS_COOLDOWN")
        return RiskDecision("REJECTED" if codes else "APPROVED", tuple(codes))
