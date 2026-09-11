from datetime import datetime


def trade_metrics(state: dict) -> dict:
    quantity = state["entry_quantity"]
    entry = state["entry_cost"] / quantity
    exit_price = state["exit_value"] / quantity
    gross = state["exit_value"] - state["entry_cost"]
    return {
        "entry_price": entry, "exit_price": exit_price, "gross_pnl": gross,
        "fees": state["fees"], "net_pnl": gross - state["fees"],
        "return_pct": (gross - state["fees"]) / state["entry_cost"] * 100,
        "holding_seconds": (datetime.fromisoformat(state["exit_time"]) - datetime.fromisoformat(state["entry_time"])).total_seconds(),
        "mae": state["mae"], "mfe": state["mfe"],
        "maximum_unrealized_loss": state["mae"], "maximum_unrealized_profit": state["mfe"],
        "entry_slippage": state["entry_slippage"], "exit_slippage": state["exit_slippage"],
        "total_slippage": state["entry_slippage"] + state["exit_slippage"],
        "entry_reason": state["entry_reason"], "exit_reason": state["exit_reason"],
    }
