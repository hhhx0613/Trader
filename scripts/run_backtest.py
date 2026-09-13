#!/usr/bin/env python3
"""
回测脚本（统一入口）

用法：
  python scripts/run_backtest.py
  python scripts/run_backtest.py --pool AAPL NVDA MSFT --start 2025-08-11 --end 2026-08-07
  python scripts/run_backtest.py --skip-vader
"""

import sys
from pathlib import Path

# 让直接运行（python scripts/xxx.py）也能找到 core 模块
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
from datetime import datetime

import pandas as pd

from core.data.market_data import fetch_ohlcv
from core.data.news_data import fetch_news
from core.indicators import compute_all_indicators
from core.multi_stock_engine import MultiStockBacktestEngine
from core.recorder import Recorder
from agents.decision_func import decide_formula_llm, decide_formula_vader
from agents.stock_selector import DEFAULT_CANDIDATE_POOL
from core.ppo.predict import PPOExposurePredictor
from core import config

# 指标预热期（日历天数）：60 天覆盖 RSI/ADX 的预热窗口
INDICATOR_WARMUP_DAYS = 60


def parse_args():
    parser = argparse.ArgumentParser(description="统一回测脚本")
    parser.add_argument("--pool", nargs="+", default=None,
                        help=f"候选股票池（默认：10 只）: {DEFAULT_CANDIDATE_POOL}")
    parser.add_argument("--start", default=None,
                        help=f"起始日期（默认：{config.DEFAULT_START_DATE}）")
    parser.add_argument("--end", default=None,
                        help=f"结束日期（默认：{config.DEFAULT_END_DATE}）")
    parser.add_argument("--skip-vader", action="store_true",
                        help="跳过 VADER 路径")
    args = parser.parse_args()

    # 默认值
    if args.pool is None:
        args.pool = DEFAULT_CANDIDATE_POOL
    if args.start is None:
        args.start = config.DEFAULT_START_DATE
    if args.end is None:
        args.end = config.DEFAULT_END_DATE

    return args


def download_data(candidate_pool, start_date, end_date):
    """下载行情数据和新闻数据"""
    print("\n" + "=" * 60)
    print("  [1/3] 数据采集")
    print("=" * 60)

    # 预热起始日：多拉 15 天给指标计算用
    warmup_start = (pd.Timestamp(start_date) - pd.Timedelta(days=INDICATOR_WARMUP_DAYS)).strftime('%Y-%m-%d')

    # --- 行情数据 ---
    print(f"\n  下载 {len(candidate_pool)} 只候选股行情（含 {INDICATOR_WARMUP_DAYS} 天预热）...")
    market_data = {}
    for symbol in candidate_pool:
        try:
            df = fetch_ohlcv(symbol=symbol, start_date=warmup_start, end_date=end_date)
            if df is not None and len(df) > 0:
                # 在扩展数据上计算指标（含预热期）
                df = compute_all_indicators(df)
                # 裁回回测区间
                df = df[df.index >= pd.Timestamp(start_date)]
                market_data[symbol] = df
                print(f"    [OK] {symbol}: {len(df)} bars "
                      f"({df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})")
            else:
                print(f"    [WARN] {symbol}: 无数据")
        except Exception as e:
            print(f"    [ERROR] {symbol}: {e}")

    if not market_data:
        raise RuntimeError("没有成功下载任何行情数据，终止")

    # --- 新闻数据 ---
    print(f"\n  下载候选股新闻...")
    all_news = []
    for symbol in candidate_pool:
        try:
            news_df = fetch_news(symbol=symbol, start_date=start_date, end_date=end_date)
            if news_df is not None and len(news_df) > 0:
                news_df["symbol"] = symbol
                all_news.append(news_df)
                print(f"    [OK] {symbol}: {len(news_df)} 条新闻")
            else:
                print(f"    [WARN] {symbol}: 无新闻")
        except Exception as e:
            print(f"    [WARN] {symbol} 新闻失败：{e}")

    if all_news:
        news_df = pd.concat(all_news, ignore_index=True).sort_values("datetime").reset_index(drop=True)
        print(f"\n  [OK] 总新闻：{len(news_df)} 条")
    else:
        news_df = pd.DataFrame(columns=["datetime", "title", "summary", "source", "sentiment_score", "symbol"])
        print(f"\n  [WARN] 无新闻数据")

    return market_data, news_df


def run_multi_stock_backtest(decide_func, market_data, news_df, candidate_pool, exposure_predictor=None):
    """用指定的决策函数跑多股票回测，返回 recorder。

    exposure_predictor 非空时挂载 PPO 择时：调仓日仅覆盖总暴露，选股/权重不变。
    """
    engine = MultiStockBacktestEngine(decide_func=decide_func, exposure_predictor=exposure_predictor)
    return engine.run(market_data, news_df, candidate_pool)


def create_buy_and_hold_recorder(market_data, initial_capital):
    """构造 Buy & Hold 等权基准：第一天等权买入全池，持有不动。"""
    symbols = list(market_data.keys())
    n = len(symbols)
    per_stock = initial_capital / n

    # 各股首日收盘价买入
    holdings = {}
    for sym in symbols:
        price = market_data[sym].iloc[0]["close"]
        holdings[sym] = per_stock / price

    # 以数据最长的股票为基准日期序列
    ref = max(market_data.values(), key=len)
    dates = ref.index

    recorder = Recorder()
    for date in dates:
        equity = 0.0
        for sym in symbols:
            df = market_data[sym]
            if date in df.index:
                equity += holdings[sym] * df.loc[date, "close"]
            else:
                equity += holdings[sym] * df.iloc[-1]["close"]
        recorder.log_equity(date, equity)

    return recorder


def save_results(output_dir, results: dict):
    """保存回测结果：summary（对比表）+ equity（净值曲线）+ trades（交易明细）。

    Args:
        results: {策略名: Recorder} 字典，按插入顺序输出。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    saved = []

    # --- 1. summary.csv：一行一个策略，核心指标对比 ---
    summary_rows = []
    for name, rec in results.items():
        if rec and rec.equity_curve:
            m = rec.evaluate()
            summary_rows.append({"策略": name, **m})

    if summary_rows:
        path = output_dir / f"summary_{ts}.csv"
        pd.DataFrame(summary_rows).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)

    # --- 2. equity.csv：合并净值曲线（方便画图） ---
    equity_dfs = []
    for name, rec in results.items():
        if rec and rec.equity_curve:
            col = name.lower().replace("&", "and").replace(" ", "_").replace("__", "_")
            df = pd.DataFrame(rec.equity_curve, columns=["date", col])
            df["date"] = pd.to_datetime(df["date"])
            equity_dfs.append(df)

    if equity_dfs:
        merged = equity_dfs[0]
        for df in equity_dfs[1:]:
            merged = merged.merge(df, on="date", how="outer")
        merged = merged.sort_values("date").reset_index(drop=True)
        path = output_dir / f"equity_{ts}.csv"
        merged.to_csv(path, index=False)
        saved.append(path.name)

    # --- 3. trades.csv：合并交易记录 ---
    all_trades = []
    for name, rec in results.items():
        if rec and rec.trades:
            for t in rec.trades:
                all_trades.append({
                    "策略": name, "日期": t.date, "股票": t.symbol,
                    "方向": t.direction, "价格": f"{t.price:.2f}", "股数": t.shares,
                    "手续费": f"{t.commission:.2f}", "盈亏": f"{t.pnl:.2f}", "原因": t.reason,
                })
    if all_trades:
        path = output_dir / f"trades_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)

    if saved:
        print(f"\n  结果已保存到 {output_dir}:")
        for name in saved:
            print(f"    - {name}")


def print_summary(results: dict):
    """打印各策略的核心指标汇总对比表。"""
    rows = []
    for name, rec in results.items():
        if rec and rec.equity_curve:
            m = rec.evaluate()
            rows.append((name, m))

    if not rows:
        return

    print(f"\n{'=' * 72}")
    print(f"  {'回测结果汇总':^68}")
    print(f"{'=' * 72}")
    print(f"  {'策略':<14} {'累计收益':>10} {'最大回撤':>10} {'夏普比率':>10} "
          f"{'胜率':>10} {'交易数':>8} {'换手率':>10}")
    print(f"  {'-' * 68}")

    for label, m in rows:
        print(f"  {label:<14} {m.get('累计收益率', 'N/A'):>10} {m.get('最大回撤', 'N/A'):>10} "
              f"{m.get('夏普比率', 'N/A'):>10} {m.get('胜率', 'N/A'):>10} "
              f"{str(m.get('总交易次数', 'N/A')):>8} {m.get('换手率', 'N/A'):>10}")

    print(f"{'=' * 72}")


def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  统一回测脚本")
    print("=" * 60)
    print(f"\n  候选池：{args.pool} ({len(args.pool)} 只)")
    print(f"  时间：{args.start} ~ {args.end}")
    print(f"  初始资金：${config.INITIAL_CAPITAL:,.0f}")
    print(f"  Top-K: {config.TOP_K}  调仓周期：{config.REBALANCE_DAYS} 天")

    # 1. 数据采集
    market_data, news_df = download_data(args.pool, args.start, args.end)

    # 2. 各策略回测
    results = {}  # {策略名: Recorder}

    # --- Buy & Hold 等权基准 ---
    print(f"\n  --- Buy & Hold 等权基准 ---")
    results["Buy & Hold"] = create_buy_and_hold_recorder(market_data, config.INITIAL_CAPITAL)
    print(f"  [OK] 等权 B&H 完成")

    # --- LLM Top-K ---
    print(f"\n  --- LLM Top-K ---")

    def llm_decide(date, market_state, candidate_pool, news_df, current_holdings, market_data=None):
        return decide_formula_llm(date, market_state, candidate_pool, news_df, current_holdings, market_data=market_data)

    llm_recorder = run_multi_stock_backtest(llm_decide, market_data, news_df, args.pool)
    if llm_recorder.equity_curve:
        print(f"  [OK] LLM 回测完成，最终权益：${llm_recorder.equity_curve[-1][1]:,.2f}")
    results["LLM Top-K"] = llm_recorder

    # --- PPO Top-K（LLM 选股 + PPO stage1 择时接管总暴露）---
    # 与 LLM Top-K 同一选股，仅把「波动率目标公式暴露」换成 PPO 学出的档位 → 干净 A/B
    print(f"\n  --- PPO Top-K ---")
    if (config.PROJECT_ROOT / "models" / "ppo_stage1.zip").exists():
        predictor = PPOExposurePredictor(args.pool, args.start, args.end)
        ppo_recorder = run_multi_stock_backtest(
            llm_decide, market_data, news_df, args.pool,
            exposure_predictor=predictor.predict_exposure,
        )
        if ppo_recorder.equity_curve:
            print(f"  [OK] PPO 回测完成，最终权益：${ppo_recorder.equity_curve[-1][1]:,.2f}")
        results["PPO Top-K"] = ppo_recorder
    else:
        print(f"  [SKIP] 未找到 models/ppo_stage1.zip（先跑 scripts/train_ppo.py --stage 1）")

    # --- VADER Top-K ---
    if not args.skip_vader:
        print(f"\n  --- VADER Top-K ---")

        def vader_decide(date, market_state, candidate_pool, news_df, current_holdings, market_data=None):
            return decide_formula_vader(date, market_state, candidate_pool, news_df, current_holdings, market_data=market_data)

        vader_recorder = run_multi_stock_backtest(vader_decide, market_data, news_df, args.pool)
        if vader_recorder.equity_curve:
            print(f"  [OK] VADER 回测完成，最终权益：${vader_recorder.equity_curve[-1][1]:,.2f}")
        results["VADER Top-K"] = vader_recorder
    else:
        print(f"\n  [SKIP] VADER")

    # 3. 保存结果
    print("\n" + "=" * 60)
    print("  保存结果")
    print("=" * 60)

    output_dir = config.OUTPUT_DIR / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    save_results(output_dir, results)

    # 4. 终端汇总
    print_summary(results)

    print("\n" + "=" * 60)
    print("  回测完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
