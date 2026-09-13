"""
PPO 数据加载（data）

把「多标的行情缓存」加载成 env 需要的 {symbol: DataFrame(含指标列)}。

与 scripts/run_backtest.download_data 的关键区别（为什么单独一份而非复用）：
  1. 不回扩预热期：run_backtest 为让指标在 start_date 当天就稳定，会多拉 60 天再裁掉；
     PPO 训练不需要——env 内部 episode 起点强制 >= _LOOKBACK(60)，前 60 天天然充当
     指标预热 + 特征回看缓冲。回扩反而会越过缓存起始日（如 2025-06-12）触发网络请求，
     破坏「离线可复现」。
  2. 只加载行情、不碰新闻：LLM 语义信号走独立面板（阶段二 llm_provider），按需前向填充。
"""

from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from core import config
from core.data.market_data import fetch_ohlcv
from core.indicators import compute_all_indicators
from core.ppo.features import compute_llm_features


def load_market_data(
    symbols: List[str],
    start_date: str,
    end_date: str,
    verbose: bool = True,
) -> Dict[str, pd.DataFrame]:
    """
    加载多标的日线行情并计算全部技术指标。

    参数：
      symbols:    股票代码列表（如 DEFAULT_CANDIDATE_POOL）
      start_date: 起始日 "YYYY-MM-DD"（应留足 >= _LOOKBACK+episode_min_days 的历史）
      end_date:   结束日 "YYYY-MM-DD"
      verbose:    是否打印每只股票的加载结果

    返回：
      {symbol: DataFrame(index=DatetimeIndex, 含 OHLCV + ema/rsi/macd/atr/adx/vwap)}
      加载失败或无数据的标的直接跳过（禁止模拟兜底，项目规范）。

    注意：返回的 DataFrame 保留完整 [start_date, end_date] 区间，不做裁剪——
    前段数据用于指标预热与特征回看，env 会从 _LOOKBACK 之后才开始 episode。
    """
    market_data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = fetch_ohlcv(symbol=sym, start_date=start_date, end_date=end_date)
        if df is None or len(df) == 0:
            if verbose:
                print(f"    [WARN] {sym}: 无数据，跳过")
            continue
        df = compute_all_indicators(df)
        market_data[sym] = df
        if verbose:
            print(f"    [OK] {sym}: {len(df)} bars "
                  f"({df.index[0].date()} ~ {df.index[-1].date()})")

    if not market_data:
        raise RuntimeError(
            f"没有成功加载任何行情数据（symbols={symbols}, {start_date}~{end_date}）；"
            f"请确认缓存存在或数据源可用")
    return market_data


def build_llm_provider(
    symbols: List[str],
    model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Callable:
    """
    构造阶段二 PIT LLM 信号 provider：callable(date, symbols) -> llm_obs(5)。

    为什么预载内存面板而非每步查 SQLite：
      env 每步（每个交易日）都调 provider，逐次查库 = 10 symbol × 250 天 × N episode 次查询，
      训练会慢到不可用。这里一次性 bulk 读入、每 symbol 存有序日期数组，查询用
      np.searchsorted 做 O(log n) 的「<= date 最近一条」前向填充。

    PIT 一致性（训练信号 == 实盘推理信号）：
      按当前 model + prompt_version 过滤——prompt_version 是 analyst 所有 prompt 文件内容的
      md5，改 prompt 即变。这样历史训练只会用到「当时那套 prompt 产出的信号」，
      绝不让重构后的 prompt 语义倒灌污染历史（否则阶段二学到的 LLM 增量是假的）。

    缺失语义：某 symbol 在 date 之前无任何信号 → 该 symbol 不贡献；全池无信号 →
    compute_llm_features 返回中性（conf/score=0, age=最陈旧），禁止乐观兜底。
    """
    import sqlite3
    from core.data.llm_cache_db import get_cache_db
    from agents.llm_analyst import load_prompts   # 延迟导入：避免 data 模块加载即拉起 LLM 依赖

    if model is None:
        model = config.DEFAULT_LLM_MODEL
    if prompt_version is None:
        _, prompt_version = load_prompts("analyst")
    if db_path is None:
        db_path = get_cache_db().db_path

    # --- bulk 只读载入（本模块自带查询：训练专用的 PIT 批量访问模式，与实盘逐条 get_cache 不同）---
    placeholders = ",".join("?" * len(symbols))
    panel: Dict[str, tuple] = {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT symbol, analysis_date, confidence, composite_score "
            f"FROM llm_analysis_cache "
            f"WHERE model=? AND prompt_version=? AND symbol IN ({placeholders}) "
            f"ORDER BY symbol, analysis_date",
            (model, prompt_version, *symbols),
        ).fetchall()

    # 组装 {symbol: (dates[np.datetime64 有序], confs, scores)}
    tmp: Dict[str, List] = {}
    for sym, date_str, conf, score in rows:
        tmp.setdefault(sym, []).append(
            (pd.Timestamp(date_str), float(conf or 0.0), float(score or 0.0)))
    for sym, lst in tmp.items():
        lst.sort(key=lambda x: x[0])   # 防御性排序（SQL 已按 analysis_date 排，字符串序==时间序）
        panel[sym] = (
            np.array([d for d, _, _ in lst], dtype="datetime64[ns]"),
            np.array([c for _, c, _ in lst], dtype=np.float64),
            np.array([s for _, _, s in lst], dtype=np.float64),
        )

    def provider(date, query_symbols: List[str]) -> np.ndarray:
        ts = np.datetime64(pd.Timestamp(date))
        signals: List[Dict] = []
        ages: List[int] = []
        for sym in query_symbols:
            entry = panel.get(str(sym))
            if entry is None:
                continue
            dates, confs, scores = entry
            i = int(np.searchsorted(dates, ts, side="right")) - 1   # 最近一条 <= date
            if i < 0:
                continue   # 该 symbol 在 date 前尚无信号
            signals.append({"confidence": confs[i], "composite_score": scores[i]})
            # signal_age：距该信号的天数（date 与信号日均归一到日粒度）
            ages.append(int((ts - dates[i]).astype("timedelta64[D]").astype(int)))
        # 池级 signal_age 取最新（min）：信号在调仓日同批生成，各 symbol age 基本一致
        age = min(ages) if ages else None
        return compute_llm_features(signals, age)

    return provider
