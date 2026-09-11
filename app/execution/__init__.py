from typing import Protocol
from app.config import Settings
from app.core.types import Bar, ExecutionResult, Fill, OrderRequest


class ExecutionEngine(Protocol):
    def execute(self, order: OrderRequest, quote: Bar) -> ExecutionResult: ...


class PaperExecutionEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def execute(self, order: OrderRequest, quote: Bar) -> ExecutionResult:
        if order.symbol != quote.symbol:
            return ExecutionResult(status="REJECTED", reason="SYMBOL_MISMATCH")
        buy = order.side == "BUY"
        price = (quote.ask if buy else quote.bid) * (1 + (1 if buy else -1) * self.settings.slippage_bps / 10000)
        if order.order_type == "LIMIT" and ((buy and price > order.limit_price) or (not buy and price < order.limit_price)):
            return ExecutionResult(status="CANCELLED", reason="IOC_LIMIT_NOT_MARKETABLE")
        quantity = min(order.quantity, quote.volume)
        if not quantity:
            return ExecutionResult(status="REJECTED", reason="NO_LIQUIDITY")
        fill = Fill(quantity=quantity, price=price, fee=quantity * self.settings.fee_per_share, timestamp=quote.timestamp)
        return ExecutionResult(status="FILLED" if quantity == order.quantity else "PARTIALLY_FILLED",
                               fills=[fill], reason=None if quantity == order.quantity else "IOC_REMAINDER_CANCELLED")


class WebullExecutionEngine:
    """Reserved for an official SDK Sandbox adapter; intentionally has no client."""
    def execute(self, order: OrderRequest, quote: Bar) -> ExecutionResult:
        raise RuntimeError("Webull execution disabled; paper simulation only")
