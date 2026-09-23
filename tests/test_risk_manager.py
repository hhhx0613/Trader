from datetime import date, timedelta

import pandas as pd

from core.multi_stock_engine import MultiStockBacktestEngine
from core.risk_manager import RiskManager


def test_daily_drawdown_halt_resets_on_next_trading_day():
    manager = RiskManager(100_000)
    first_day = date(2026, 1, 5)

    halted = manager.check_risk(
        current_date=first_day,
        current_equity=94_000,
        holding_shares=0,
        entry_price=0.0,
        current_price=0.0,
        day_start_equity=100_000,
    )
    assert not halted["allow_buy"]

    next_day = manager.check_risk(
        current_date=first_day + timedelta(days=1),
        current_equity=94_000,
        holding_shares=0,
        entry_price=0.0,
        current_price=0.0,
        day_start_equity=94_000,
    )
    assert next_day["allow_buy"]


def test_atr_stop_only_marks_the_triggering_symbol():
    current_date = pd.Timestamp("2026-01-05")
    engine = MultiStockBacktestEngine(lambda **_: {"holdings": [], "exposure": 0.0})
    engine.holdings = {
        "STOP": {"shares": 10, "entry_price": 100.0},
        "KEEP": {"shares": 10, "entry_price": 100.0},
    }
    market_data = {
        "STOP": pd.DataFrame({"open": [90.0], "close": [90.0], "atr": [2.0]}, index=[current_date]),
        "KEEP": pd.DataFrame({"open": [110.0], "close": [110.0], "atr": [2.0]}, index=[current_date]),
    }

    status = engine._check_risk(current_date, 10_000.0, market_data, 10_000.0)

    assert status["stop_orders"] and status["stop_orders"][0][0] == "STOP"
    assert all(symbol != "KEEP" for symbol, _ in status["stop_orders"])
