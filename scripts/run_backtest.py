#!/usr/bin/env python3
"""
回测脚本（统一入口）

用法：
  python scripts/run_backtest.py
  python scripts/run_backtest.py --with-ppo
  python scripts/run_backtest.py --pool AAPL NVDA MSFT --start 2025-08-11 --end 2026-08-07
  python scripts/run_backtest.py --skip-rule --skip-vader
"""

import sys
from pathlib import Path

# 让直接运行（python scripts/xxx.py）也能找到 core 模块
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
import numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from core.data.market_data import fetch_ohlcv
from core.data.news_data import fetch_news
from core.indicators import compute_all_indicators
from core.strategy import generate_signals
from core.backtest_engine import BacktestEngine
from core.multi_stock_engine import MultiStockBacktestEngine
from agents.decision_func import decide_formula_llm, decide_formula_vader
from agents.stock_selector import DEFAULT_CANDIDATE_POOL
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
    parser.add_argument("--with-ppo", action="store_true",
                        help="启用 PPO 路径")
    parser.add_argument("--ppo-mode", choices=["small", "medium", "large"],
                        default="medium", help="PPO 模型规模")
    parser.add_argument("--skip-rule", action="store_true",
                        help="跳过规则基线")
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
    print("  [1/4] 数据采集")
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


def run_multi_stock_backtest(decide_func, market_data, news_df, candidate_pool, label=""):
    """
    用指定的决策函数跑多股票回测。
    返回 recorder
    """
    engine = MultiStockBacktestEngine(decide_func=decide_func)
    recorder = engine.run(market_data, news_df, candidate_pool)
    return recorder


def save_results(output_dir, rule_results=None, llm_recorder=None, 
                 vader_recorder=None, ppo_recorder=None):
    """保存所有路径的结果到 output/ 目录"""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    saved = []

    # 规则基线
    if rule_results:
        for sym, metrics in rule_results.items():
            path = output_dir / f"rule_{sym}_{ts}.csv"
            pd.DataFrame([metrics]).to_csv(path, index=False, encoding="utf-8-sig")
            saved.append(str(path.name))

    # LLM
    if llm_recorder and llm_recorder.trades:
        trades = [{
            "日期": t.date, "股票": t.symbol, "方向": t.direction,
            "价格": f"{t.price:.2f}", "股数": t.shares,
            "手续费": f"{t.commission:.2f}", "盈亏": f"{t.pnl:.2f}", "原因": t.reason,
        } for t in llm_recorder.trades]
        path = output_dir / f"llm_trades_{ts}.csv"
        pd.DataFrame(trades).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)
    if llm_recorder and llm_recorder.equity_curve:
        path = output_dir / f"llm_equity_{ts}.csv"
        pd.DataFrame(llm_recorder.equity_curve, columns=["date", "equity"]).to_csv(path, index=False)
        saved.append(path.name)

    # VADER
    if vader_recorder and vader_recorder.trades:
        trades = [{
            "日期": t.date, "股票": t.symbol, "方向": t.direction,
            "价格": f"{t.price:.2f}", "股数": t.shares,
            "手续费": f"{t.commission:.2f}", "盈亏": f"{t.pnl:.2f}", "原因": t.reason,
        } for t in vader_recorder.trades]
        path = output_dir / f"vader_trades_{ts}.csv"
        pd.DataFrame(trades).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)
    if vader_recorder and vader_recorder.equity_curve:
        path = output_dir / f"vader_equity_{ts}.csv"
        pd.DataFrame(vader_recorder.equity_curve, columns=["date", "equity"]).to_csv(path, index=False)
        saved.append(path.name)

    # PPO
    if ppo_recorder and ppo_recorder.trades:
        trades = [{
            "日期": t.date, "股票": t.symbol, "方向": t.direction,
            "价格": f"{t.price:.2f}", "股数": t.shares,
            "手续费": f"{t.commission:.2f}", "盈亏": f"{t.pnl:.2f}", "原因": t.reason,
        } for t in ppo_recorder.trades]
        path = output_dir / f"ppo_trades_{ts}.csv"
        pd.DataFrame(trades).to_csv(path, index=False, encoding="utf-8-sig")
        saved.append(path.name)
    if ppo_recorder and ppo_recorder.equity_curve:
        path = output_dir / f"ppo_equity_{ts}.csv"
        pd.DataFrame(ppo_recorder.equity_curve, columns=["date", "equity"]).to_csv(path, index=False)
        saved.append(path.name)

    if saved:
        print(f"\n  结果已保存到 {output_dir}:")
        for name in saved:
            print(f"    - {name}")


def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  统一回测脚本")
    print("=" * 60)
    print(f"\n  候选池：{args.pool} ({len(args.pool)} 只)")
    print(f"  时间：{args.start} ~ {args.end}")
    print(f"  初始资金：${config.INITIAL_CAPITAL:,.0f}")
    print(f"  Top-K: {config.TOP_K}  调仓周期：{config.REBALANCE_DAYS} 天")
    if args.with_ppo:
        print(f"  PPO 模式：{args.ppo_mode}")
    else:
        print(f"  PPO 模式：禁用")

    # 1. 数据采集
    market_data, news_df = download_data(args.pool, args.start, args.end)

    # 2. 路径 A: 规则基线（单股回测）
    rule_results = None
    if not args.skip_rule:
        print("\n" + "=" * 60)
        print("  [2/4] 路径 A: 规则基线（单股回测）")
        print("=" * 60)

        rule_results = {}

        def _backtest_single_stock(symbol, df):
            """对单只股票跑规则基线回测（供线程池调用）。"""
            df_enriched = generate_signals(df.copy())

            buy_count = (df_enriched["signal"] == config.SIGNAL_BUY).sum()
            sell_count = (df_enriched["signal"] == config.SIGNAL_SELL).sum()
            print(f"    信号：买入 {buy_count} 次，卖出 {sell_count} 次")

            engine = BacktestEngine()
            recorder = engine.run(df_enriched, symbol=symbol)
            metrics = recorder.evaluate()

            bh = recorder.evaluate_buy_and_hold(df)
            if bh:
                metrics.update(bh)

            print(f"    累计收益率：{metrics.get('累计收益率', 'N/A')}")
            return symbol, metrics

        with ThreadPoolExecutor(max_workers=min(4, len(market_data))) as executor:
            futures = {
                executor.submit(_backtest_single_stock, symbol, df): symbol
                for symbol, df in market_data.items()
            }
            for future in as_completed(futures):
                symbol, metrics = future.result()
                rule_results[symbol] = metrics

        print(f"\n  [OK] 规则基线完成：{len(rule_results)} 只股票")
    else:
        print("\n  [SKIP] 路径 A: 规则基线")

    # 3. 路径 B/C: LLM/VADER 多股回测
    print("\n" + "=" * 60)
    print("  [3/4] 路径 B/C: LLM 选股 / VADER 选股")
    print("=" * 60)

    llm_recorder = None
    vader_recorder = None

    # --- 路径 B: LLM ---
    print(f"\n  --- 路径 B: LLM Top-K ---")

    def llm_decide(date, market_state, candidate_pool, news_df, current_holdings, market_data=None):
        return decide_formula_llm(date, market_state, candidate_pool, news_df, current_holdings, market_data=market_data)

    llm_recorder = run_multi_stock_backtest(
        llm_decide, market_data, news_df, args.pool, label="LLM"
    )
    if llm_recorder.equity_curve:
        print(f"  [OK] LLM 回测完成，最终权益：${llm_recorder.equity_curve[-1][1]:,.2f}")

    # --- 路径 C: VADER ---
    if not args.skip_vader:
        print(f"\n  --- 路径 C: VADER Top-K ---")

        def vader_decide(date, market_state, candidate_pool, news_df, current_holdings, market_data=None):
            return decide_formula_vader(date, market_state, candidate_pool, news_df, current_holdings, market_data=market_data)

        vader_recorder = run_multi_stock_backtest(
            vader_decide, market_data, news_df, args.pool, label="VADER"
        )
        if vader_recorder.equity_curve:
            print(f"  [OK] VADER 回测完成，最终权益：${vader_recorder.equity_curve[-1][1]:,.2f}")
    else:
        print(f"\n  [SKIP] 路径 C: VADER")

    # 4. 路径 D: PPO (可选)
    ppo_recorder = None
    if args.with_ppo:
        print("\n" + "=" * 60)
        print("  [4/4] 路径 D: PPO 仓位融合器")
        print("=" * 60)
        
        try:
            from core.ppo.ppo_env import PPOEnv
            from core.ppo.ppo_policy import PPOPolicy
            from core.ppo.ppo_trainer import PPOTrainer
            
            print(f"\n  [INFO] PPO 功能已启用，但需要先完成阶段 5 开发")
            print(f"  [WARN] 当前版本暂不支持 PPO，跳过此路径")
            # TODO: 实现 PPO 路径
        except ImportError as e:
            print(f"  [ERROR] PPO 模块导入失败：{e}")
    else:
        print("\n  [SKIP] 路径 D: PPO (未启用)")

    # 5. 保存结果
    print("\n" + "=" * 60)
    print("  [5/5] 保存结果")
    print("=" * 60)

    output_dir = config.OUTPUT_DIR / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    save_results(output_dir, rule_results, llm_recorder, vader_recorder, ppo_recorder)

    print("\n" + "=" * 60)
    print("  回测完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
