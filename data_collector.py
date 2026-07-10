"""
数据采集模块（DataCollector）

职责：
  1. 按优先级从四个数据源获取 K 线数据：本地缓存 → yfinance → Alpha Vantage → IBKR
  2. 对原始数据做清洗和标准化，输出统一的 OHLCV DataFrame
  3. 将下载的数据缓存到本地 CSV，避免重复请求

设计说明（四层兜底）：
  - 本地缓存：最快、最稳，有缓存就不走网络
  - yfinance：主力免费数据源，覆盖面广
  - Alpha Vantage：备用，需免费 API Key，有调用次数限制
  - IBKR API：最终兜底 + 实盘行情源（阶段5正式启用，阶段1预留接口）
"""

import os
import numpy as np
import pandas as pd
import requests
from pathlib import Path
from typing import Optional

import config

# ==================== 公开接口 ====================

def fetch_ohlcv(
    symbol: str = config.DEFAULT_SYMBOL,
    start_date: str = config.DEFAULT_START_DATE,
    end_date: str = config.DEFAULT_END_DATE,
) -> pd.DataFrame:
    """
    获取指定标的的日线 OHLCV 数据，自动三层兜底。

    参数：
        symbol     : 股票代码，如 "AAPL"、"TSLA"
        start_date : 起始日期，格式 "YYYY-MM-DD"
        end_date   : 结束日期，格式 "YYYY-MM-DD"

    返回：
        pd.DataFrame，列 = [open, high, low, close, volume]，index = DatetimeIndex

    兜底顺序：
        1. 本地 CSV 缓存（最快，无网络依赖）
        2. yfinance（主力网络源）
        3. Alpha Vantage（备用网络源）
        4. IBKR API（最终兜底 + 实盘行情，需本地运行 TWS/IB Gateway）
    """
    # 生成缓存文件名：symbol + 日期范围，保证不同参数不会混用
    cache_filename = f"{symbol}_{start_date}_{end_date}.csv"
    cache_path = config.CACHE_DIR / cache_filename

    # --- 第 1 层：检查本地缓存 ---
    df = _try_load_cache(cache_path)
    if df is not None:
        print(f"[DataCollector] 命中本地缓存: {cache_filename}")
        return df

    # --- 第 2 层：yfinance ---
    df = _try_yfinance(symbol, start_date, end_date)
    if df is not None:
        print(f"[DataCollector] yfinance 获取成功: {symbol}, {len(df)} 条记录")
        _save_cache(df, cache_path)  # 成功后缓存到本地
        return df


    # --- 第 3 层：Alpha Vantage ---
    df = _try_alpha_vantage(symbol, start_date, end_date)
    if df is not None:
        print(f"[DataCollector] Alpha Vantage 获取成功: {symbol}, {len(df)} 条记录")
        _save_cache(df, cache_path)
        return df

    # --- 第 4 层：IBKR API（最终兜底，需本地运行 TWS 或 IB Gateway）---
    df = _try_ibkr(symbol, start_date, end_date)
    if df is not None:
        print(f"[DataCollector] IBKR 获取成功: {symbol}, {len(df)} 条记录")
        _save_cache(df, cache_path)
        return df
    
    # --- 开发兜底：生成模拟数据（仅用于无网络环境下的开发调试）---
    print(f"[DataCollector] ⚠️  所有数据源均不可用，生成模拟数据用于开发测试")
    print(f"[DataCollector]    正式上线前请确保至少一个网络数据源可用")
    df = _generate_sample_data(symbol, start_date, end_date)
    _save_cache(df, cache_path)
    return df


# ==================== 各数据源实现 ====================

def _try_load_cache(cache_path: Path) -> Optional[pd.DataFrame]:
    """
    尝试从本地 CSV 读取缓存数据。
    """
    if not cache_path.exists():
        return None

    try:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        # 基本校验：必须有 OHLCV 五列
        required_cols = {"open", "high", "low", "close", "volume"}
        if not required_cols.issubset(df.columns):
            print(f"[DataCollector] 缓存文件列不完整，跳过: {cache_path}")
            return None
        return df
    except Exception as e:
        print(f"[DataCollector] 读取缓存失败: {e}")
        return None


def _try_yfinance(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """
    通过 yfinance 下载日线数据。
    返回的列名统一为小写（open, high, low, close, volume）。
    """
    try:
        # Windows 上 curl_cffi SSL 证书路径问题，禁用验证即可
        try:
            import curl_cffi.requests.session as _s
            _o = _s.BaseSession.__init__
            _s.BaseSession.__init__ = lambda self, *a, **k: _o(self, *a, verify=False, **{x: v for x, v in k.items() if x != 'verify'})
        except ImportError:
            pass

        import yfinance as yf

        df = yf.Ticker(symbol).history(
            start=start_date, end=end_date, auto_adjust=True
        )

        if df.empty:
            print(f"[DataCollector] yfinance 返回空数据: {symbol}")
            return None

        # 标准化列名：大写 → 小写，只保留 OHLCV 五列
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        df = df[["open", "high", "low", "close", "volume"]].dropna()

        return df

    except ImportError:
        print("[DataCollector] yfinance 未安装，跳过。请运行: pip install yfinance")
        return None
    except Exception as e:
        print(f"[DataCollector] yfinance 获取失败: {e}")
        return None




def _try_alpha_vantage(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """
    通过 Alpha Vantage TIME_SERIES_DAILY 接口获取日线数据（兜底方案）。

    注意事项：
      - 免费版每日限 25 次请求，每分钟限 5 次
      - 需要设置 ALPHA_VANTAGE_API_KEY（在 config.py 或环境变量中）
      - 免费版 outputsize=compact 只返回最近 100 条，超出范围的数据拿不到
    """
    api_key = config.ALPHA_VANTAGE_API_KEY
    if not api_key:
        print("[DataCollector] Alpha Vantage API Key 未配置，跳过")
        return None

    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=TIME_SERIES_DAILY"
            f"&symbol={symbol}"
            f"&outputsize=compact"
            f"&apikey={api_key}"
        )
        response = requests.get(url, timeout=30, verify=False)
        response.raise_for_status()
        data = response.json()

        # Alpha Vantage 的日线数据在 "Time Series (Daily)" 键下
        ts_key = "Time Series (Daily)"
        if ts_key not in data:
            print(f"[DataCollector] Alpha Vantage 返回格式异常: {list(data.keys())}")
            return None

        # 解析 JSON → DataFrame
        records = []
        for date_str, values in data[ts_key].items():
            records.append({
                "date": date_str,
                "open": float(values["1. open"]),
                "high": float(values["2. high"]),
                "low": float(values["3. low"]),
                "close": float(values["4. close"]),
                "volume": float(values["5. volume"]),
            })

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        # 按日期范围过滤
        df = df.loc[start_date:end_date]

        if df.empty:
            return None

        return df

    except Exception as e:
        print(f"[DataCollector] Alpha Vantage 获取失败: {e}")
        return None


def _try_ibkr(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """
    通过 IBKR API（TWS / IB Gateway）获取历史 K 线数据。

    这是 Plan 中规定的最终兜底数据源，同时也是阶段5实盘模拟的行情源。

    前置条件：
      - 本地已安装并运行 TWS (Trader Workstation) 或 IB Gateway
      - 已安装 ib_insync 库：pip install ib_insync
      - TWS/IB Gateway 已登录并开启 API 连接（默认端口 7497）

    阶段1 状态：预留接口，如果本地没有运行 TWS 则直接跳过。
    阶段5 将正式启用此数据源，用于实时行情订阅和 Paper Trading。
    """
    try:
        from ib_insync import IB, Stock, util

        # 尝试连接本地 TWS/IB Gateway
        # 常见端口：7497 = TWS 模拟盘，4002 = IB Gateway 模拟盘，4001 = IB Gateway 实盘
        ib = IB()
        connected = False
        for port in (config.IBKR_PORT, 7497, 4001):
            try:
                ib.connect("127.0.0.1", port, clientId=1, timeout=5)
                if ib.isConnected():
                    connected = True
                    break
            except Exception:
                continue

        if not connected:
            print("[DataCollector] IBKR 未运行（TWS/IB Gateway），跳过")
            return None

        # 定义合约
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        # 请求历史数据
        # durationStr: 时间跨度（如 "3 Y" = 3年）
        # barSizeSetting: K线粒度（"1 day" = 日线）
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=f"{(pd.Timestamp(end_date) - pd.Timestamp(start_date)).days} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,  # 仅常规交易时段
            formatDate=1,
        )

        ib.disconnect()

        if not bars:
            return None

        # 转换为 DataFrame
        df = util.df(bars)
        df = df.rename(columns={
            "date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume",
        })
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df.loc[start_date:end_date]
        df = df[["open", "high", "low", "close", "volume"]].dropna()

        return df if not df.empty else None

    except ImportError:
        # ib_insync 未安装，阶段1不需要安装，静默跳过
        print("[DataCollector] ib_insync 未安装，IBKR 数据源跳过（阶段5再启用）")
        return None
    except Exception as e:
        print(f"[DataCollector] IBKR 获取失败: {e}")
        return None


def _generate_sample_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    生成模拟的股票日线数据，作为最终兜底方案。

    使用几何布朗运动（GBM）模拟股价走势，生成的数据具有真实股票的统计特性：
    - 年化收益率约 10%
    - 年化波动率约 25%
    - 日内波动（high-low）约为收盘价的 1-3%

    ⚠️ 注意：模拟数据仅用于开发和调试，不代表真实市场表现。
    """
    np.random.seed(42)  # 固定随机种子，保证结果可复现

    dates = pd.bdate_range(start=start_date, end=end_date)  # 交易日（排除周末）
    n = len(dates)

    # GBM 参数
    mu = 0.10 / 252       # 日收益率（年化 10%）
    sigma = 0.25 / np.sqrt(252)  # 日波动率（年化 25%）

    # 生成对数收益率序列
    log_returns = np.random.normal(mu, sigma, n)
    # 累积得到价格序列（初始价格设为 150，接近 AAPL 的价格范围）
    prices = 150 * np.exp(np.cumsum(log_returns))

    # 生成 OHLCV 数据
    daily_range = np.abs(np.random.normal(0.015, 0.005, n))  # 日内波动幅度
    high = prices * (1 + daily_range / 2)
    low = prices * (1 - daily_range / 2)
    open_prices = low + np.random.uniform(0, 1, n) * (high - low)
    volume = np.random.randint(1_000_000, 50_000_000, n).astype(float)

    df = pd.DataFrame({
        "open": open_prices,
        "high": high,
        "low": low,
        "close": prices,
        "volume": volume,
    }, index=dates)
    df.index.name = "Date"

    return df


# ==================== 缓存写入 ====================

def _save_cache(df: pd.DataFrame, cache_path: Path) -> None:
    """将 DataFrame 保存为 CSV 缓存，供下次直接读取。"""
    try:
        df.to_csv(cache_path)
        print(f"[DataCollector] 已缓存到: {cache_path.name}")
    except Exception as e:
        print(f"[DataCollector] 缓存写入失败（不影响本次使用）: {e}")
