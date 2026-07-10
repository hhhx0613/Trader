"""
测试技术指标计算模块
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import indicators


def _make_sample_df(n=100):
    """生成测试用 OHLCV 数据"""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=n)
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    return pd.DataFrame({
        "open": prices + np.random.randn(n) * 0.2,
        "high": prices + abs(np.random.randn(n) * 0.5),
        "low": prices - abs(np.random.randn(n) * 0.5),
        "close": prices,
        "volume": np.random.randint(1_000_000, 10_000_000, n).astype(float),
    }, index=dates)


def test_compute_ma():
    df = indicators.compute_ma(_make_sample_df())
    assert "ma_short" in df.columns
    assert "ma_long" in df.columns
    assert df["ma_short"].notna().all()
    print("  compute_ma OK")


def test_compute_ema():
    df = indicators.compute_ema(_make_sample_df())
    assert "ema_short" in df.columns
    assert "ema_long" in df.columns
    print("  compute_ema OK")


def test_compute_rsi():
    df = indicators.compute_rsi(_make_sample_df())
    assert "rsi" in df.columns
    assert (df["rsi"] >= 0).all() and (df["rsi"] <= 100).all()
    print("  compute_rsi OK")


def test_compute_macd():
    df = indicators.compute_macd(_make_sample_df())
    assert all(c in df.columns for c in ["macd_line", "macd_signal", "macd_hist"])
    print("  compute_macd OK")


def test_compute_atr():
    df = indicators.compute_atr(_make_sample_df())
    assert "atr" in df.columns
    assert (df["atr"] >= 0).all()
    print("  compute_atr OK")


def test_compute_vwap():
    df = indicators.compute_vwap(_make_sample_df())
    assert "vwap" in df.columns
    assert df["vwap"].notna().all()
    print("  compute_vwap OK")


def test_compute_all():
    df = indicators.compute_all_indicators(_make_sample_df())
    expected_cols = {"ma_short", "ma_long", "ema_short", "ema_long",
                     "rsi", "macd_line", "macd_signal", "macd_hist", "atr", "vwap"}
    assert expected_cols.issubset(df.columns)
    print(f"  compute_all: {len(df.columns)} columns OK")


if __name__ == "__main__":
    print("=== test_indicators ===")
    test_compute_ma()
    test_compute_ema()
    test_compute_rsi()
    test_compute_macd()
    test_compute_atr()
    test_compute_vwap()
    test_compute_all()
    print("All passed!")
