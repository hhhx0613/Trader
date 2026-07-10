"""
测试数据采集模块
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import data_collector


def test_fetch_from_cache_or_yfinance():
    """测试基本数据拉取流程"""
    df = data_collector.fetch_ohlcv("AAPL", "2025-06-01", "2025-06-30")
    assert df is not None
    assert len(df) > 0
    assert set(df.columns) >= {"open", "high", "low", "close", "volume"}
    print(f"  fetch_ohlcv: {len(df)} rows OK")


def test_yfinance_directly():
    """直接测试 yfinance 数据源"""
    df = data_collector._try_yfinance("MSFT", "2025-06-01", "2025-06-30")
    assert df is not None
    assert len(df) > 0
    print(f"  yfinance: {len(df)} rows OK")


if __name__ == "__main__":
    print("=== test_data_collector ===")
    test_fetch_from_cache_or_yfinance()
    test_yfinance_directly()
    print("All passed!")
