import pandas as pd
import pytest

from core.portfolio.runtime import update_risk_state


def _frame(final_close: float) -> pd.DataFrame:
    closes = [100.0] * 29 + [final_close]
    return pd.DataFrame({
        "open": closes,
        "high": [price + 1 for price in closes],
        "low": [price - 1 for price in closes],
        "close": closes,
        "volume": [1_000_000] * len(closes),
    })


def test_update_risk_state_is_replayable_latches_lock_and_does_not_mutate_account():
    account = {
        "cash": 100.0,
        "positions": [{"symbol": "AAPL", "quantity": 1, "avg_cost": 100.0}],
        "risk_state": {"peak_equity": 200.0, "risk_locked": False},
    }

    state = update_risk_state(account, {"AAPL": _frame(70.0)}, drawdown_lock=0.10)

    assert state["peak_equity"] == 200.0
    assert state["drawdown"] == pytest.approx(0.15)
    assert state["stop_loss"] == ["AAPL"]
    assert state["risk_locked"] is True
    assert account["risk_state"] == {"peak_equity": 200.0, "risk_locked": False}


def test_update_risk_state_fails_closed_when_an_open_position_has_no_price_frame():
    account = {"cash": 100.0, "positions": [{"symbol": "AAPL", "quantity": 1, "avg_cost": 100.0}]}
    with pytest.raises(ValueError, match="missing PIT price frame"):
        update_risk_state(account, {}, drawdown_lock=0.10)
