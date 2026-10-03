"""主线行情采集器 / Mainline market collector

口径（与讨论定稿一致）：
  - K 线是"连续素材"不是"逐条事实"：按批次（一次请求一行）存 market.db，
    不逐根建 EvidenceCard——避免断连续性、避免几百张没人引用的卡
  - 每轮 capture 从批次序列现算指标（复用 core/indicators.py），产出
    每标的一张"特征卡"（kind=market）：那才是 Market Agent 可引用的事实
  - 每根 bar 的可得时间 = 它所在批次的网络接收时刻；算特征时只喂
    available_at <= as_of 的行，回放历史轮次不会偷看未来
  - coverage 表记录"哪天问过哪个日期区间"，重复请求直接库内重放
"""

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from .. import config
from ..indicators import compute_all_indicators
from ..research.providers import canonical_content_hash, record_from_payload
from ..research.raw import RawPayloadStore

# 特征计算的最小热身长度（ADX/EMA21 等指标的最长窗口约 42 根，取整 60）
_MIN_WARMUP_BARS = 60
INDICATORS_VERSION = "indicators-v1"


def _default_store() -> RawPayloadStore:
    return RawPayloadStore(config.RESEARCH_RAW_DIR)


def _bar_row(ts: pd.Timestamp, values) -> Dict[str, float]:
    return {
        "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
        "open": round(float(values["open"]), 6), "high": round(float(values["high"]), 6),
        "low": round(float(values["low"]), 6), "close": round(float(values["close"]), 6),
        "volume": float(values["volume"]),
    }


def _fetch_bars_yfinance(symbol: str, start_date: str, end_date: str) -> Optional[List[Dict[str, float]]]:
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(start=start_date, end=end_date, auto_adjust=True)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return [_bar_row(ts, row) for ts, row in df.iterrows()]
    except Exception as e:
        print(f"[Market] yfinance 失败 {symbol} {start_date}~{end_date}：{e}")
        return None


def _fetch_bars_alpha_vantage(symbol: str, start_date: str, end_date: str) -> Optional[List[Dict[str, float]]]:
    api_key = config.ALPHA_VANTAGE_API_KEY
    if not api_key:
        return None
    try:
        import requests

        response = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "TIME_SERIES_DAILY", "symbol": symbol,
                    "outputsize": "full", "apikey": api_key},
            timeout=30,
        )
        response.raise_for_status()
        series = response.json().get("Time Series (Daily)")
    except Exception as e:
        print(f"[Market] Alpha Vantage 失败 {symbol}：{e}")
        return None
    if not series:
        return None
    rows = []
    for date_str, values in sorted(series.items()):
        if not (start_date <= date_str <= end_date):
            continue
        rows.append({"date": date_str,
                     "open": float(values["1. open"]), "high": float(values["2. high"]),
                     "low": float(values["3. low"]), "close": float(values["4. close"]),
                     "volume": float(values["5. volume"])})
    return rows or None


def ensure_market_coverage(symbol: str, start_date: str, end_date: str, *,
                           store: Optional[RawPayloadStore] = None) -> None:
    """保证 [start,end] 的 K 线已在 market.db：未问过的区间走网络拉批次。

    批次记录不进 Snapshot（它是素材不是事实），所以这里自己落库并记台账；
    先 put 后 mark，崩溃只多烧一次请求（哈希去重兜底），不谎报。
    """
    store = store or _default_store()
    covered = any(ws <= start_date and we >= end_date for ws, we in store.covered("market", symbol))
    if covered:
        return

    fetched_at = datetime.now(timezone.utc)
    bars = _fetch_bars_yfinance(symbol, start_date, end_date)
    source = "yfinance"
    if bars is None:
        bars = _fetch_bars_alpha_vantage(symbol, start_date, end_date)
        source = "alpha_vantage"
    if bars is None:
        print(f"[Market] {symbol} {start_date}~{end_date} 两源均失败，不记 coverage")
        return

    record = _bars_batch_record(symbol, source, bars, fetched_at)
    store.put(record, source=source, received_at=fetched_at)
    store.mark_coverage("market", symbol=symbol, window_start=bars[0]["date"], window_end=bars[-1]["date"],
                        source=source, fetched_at=fetched_at, row_count=len(bars))
    print(f"[Market] {symbol}：新拉 {len(bars)} 根 bar（{source}，{start_date}~{end_date}）")


def _bars_batch_record(symbol: str, source: str, bars: List[Dict[str, float]],
                       fetched_at: datetime) -> Dict[str, Any]:
    """批次 payload：字段布局与 news 记录同构，保证 builder/重放哈希稳定。"""
    record = {
        "kind": "market",
        "type": "daily_bars",
        "symbol": symbol.upper(),
        "source": source,
        "title": f"{symbol.upper()} daily bars {bars[0]['date']}~{bars[-1]['date']}",
        "summary": "",
        "body": json.dumps(bars, sort_keys=True),
        "url": None,
        "window_start": bars[0]["date"],
        "window_end": bars[-1]["date"],
        "published_at": datetime.strptime(bars[-1]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc),
        "available_at": fetched_at,
        "provider_id": f"{source}:{symbol.upper()}:{bars[0]['date']}:{bars[-1]['date']}",
    }
    record["content_hash"] = canonical_content_hash(record)
    return record


def load_price_frame(symbol: str, start_date: str, end_date: str, *,
                     store: Optional[RawPayloadStore] = None) -> pd.DataFrame:
    """从批次正文重放价格帧：index=date，列 = OHLCV + available_at。

    同一天的 bar 若被多个批次覆盖（区间重叠），保留最早可得时间——
    PIT 语义下"第一次可得的时刻"才是它真正的 available_at。
    """
    store = store or _default_store()
    payloads = [p for p in store.payloads_in_range("market", symbol, start_date, end_date)
                if p.get("type") == "daily_bars"]
    earliest: Dict[str, datetime] = {}
    rows: Dict[str, Dict[str, float]] = {}
    for payload in payloads:
        available = record_from_payload(payload)["available_at"]
        for bar in json.loads(payload["body"]):
            date = bar["date"]
            if not (start_date <= date <= end_date):
                continue
            known = earliest.get(date)
            if known is None or available < known:
                earliest[date] = available
            rows.setdefault(date, {k: v for k, v in bar.items() if k != "date"})
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    frame.index = pd.to_datetime(frame.index)
    frame["available_at"] = [earliest[d.strftime("%Y-%m-%d")] for d in frame.index]
    return frame.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})


def _clean_number(value: Any) -> Optional[float]:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 6)


def build_feature_card(symbol: str, as_of: datetime, *, lookback_days: int = 220,
                       store: Optional[RawPayloadStore] = None) -> Optional[Dict[str, Any]]:
    """每标的每周期的特征卡：程序对截至 as_of 的序列算指标，产出可引用事实。

    available_at = 所用 bar 的最晚可得时间（不是计算时刻）——特征在输入
    齐备的那一刻就已存在，这样回放历史轮次时卡片语义不漂移。
    """
    store = store or _default_store()
    start = (as_of - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = as_of.strftime("%Y-%m-%d")
    frame = load_price_frame(symbol, start, end, store=store)
    if frame.empty:
        return None
    # 时点安全：只用 as_of 时已可得的行
    frame = frame[[av <= as_of for av in frame["available_at"]]]
    if len(frame) < _MIN_WARMUP_BARS:
        print(f"[Market] {symbol}: 可用 bar 仅 {len(frame)} 根（<{_MIN_WARMUP_BARS}），本轮不出特征卡")
        return None

    indicators = compute_all_indicators(frame[["open", "high", "low", "close", "volume"]])
    last = indicators.iloc[-1]
    closes = frame["close"]
    daily_returns = closes.pct_change().dropna()
    features = {name: _clean_number(last[name]) for name in indicators.columns}
    features["volatility_20d_annualized"] = _clean_number(daily_returns.tail(20).std() * math.sqrt(252))
    features["return_5d"] = _clean_number(closes.iloc[-1] / closes.iloc[-6] - 1) if len(closes) > 5 else None
    features["trend_ema_short_above_long"] = bool(last["ema_short"] > last["ema_long"])

    last_bar_date = frame.index[-1].strftime("%Y-%m-%d")
    available_at = max(frame["available_at"])
    record = {
        "kind": "market",
        "type": "feature",
        "symbol": symbol.upper(),
        "source": f"program:{INDICATORS_VERSION}",
        "title": f"{symbol.upper()} market features as of {last_bar_date}",
        "summary": "",
        "body": json.dumps(features, sort_keys=True),
        "url": None,
        "window_start": frame.index[0].strftime("%Y-%m-%d"),
        "window_end": last_bar_date,
        "published_at": frame.index[-1].to_pydatetime().replace(tzinfo=timezone.utc),
        "available_at": available_at,
        "provider_id": f"feature:{symbol.upper()}:{last_bar_date}:{INDICATORS_VERSION}",
    }
    record["content_hash"] = canonical_content_hash(record)
    store.put(record, source=record["source"], received_at=available_at)
    return record
