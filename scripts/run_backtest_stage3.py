"""
阶段 1-3 端到端验证脚本

一条命令跑通全部已完成的流程：
  1. 数据采集（行情 + 新闻）            ← 阶段 1
  2. LLM / VADER 情绪分析              ← 阶段 2
  3. Top-K 选股 + 回测 + 风控           ← 阶段 3

同时跑规则基线作为对照组，最后输出统一对比表。

三条决策路径：
  - 规则基线：MA/RSI/MACD 多条件投票（main.py 逻辑）
  - LLM 选股：LLM 分析新闻 → Top-K 等权 → 公式仓位
  - VADER 选股：VADER 情绪分 → Top-K 等权 → 公式仓位

用法：
  python scripts/run_backtest_stage3.py
  python scripts/run_backtest_stage3.py --pool AAPL NVDA MSFT --start 2026-07-07 --end 2026-08-06
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
from core.data.sentiment import analyze_news_sentiment
from core.indicators import compute_all_indicators
from core.strategy import generate_signals
from core.backtest_engine import BacktestEngine
from core.multi_stock_engine import MultiStockBacktestEngine
from agents.decision_func import decide_formula_llm, decide_formula_vader
from core import config


# ==================== 数据下载 ====================

def download_data(candidate_pool, start_date, end_date):
    """
    下载回测所需的全部数据（行情 + 新闻），只下载一次。

    返回：
      market_data: Dict[symbol, DataFrame]
      news_df: DataFrame（含 symbol 列）
    """
    print("\n" + "=" * 60)
    print("  [1/4] 数据采集（阶段 1）")
    print("=" * 60)

    # --- 行情数据 ---
    print(f"\n  下载 {len(candidate_pool)} 只候选股行情...")
    market_data = {}
    for symbol in candidate_pool:
        try:
            df = fetch_ohlcv(symbol=symbol, start_date=start_date, end_date=end_date)
            if df is not None and len(df) > 0:
                market_data[symbol] = df
                print(f"    [OK] {symbol}：{len(df)} bars "
                      f"({df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})")
            else:
                print(f"    [WARN] {symbol}：无数据")
        except Exception as e:
            print(f"    [ERROR] {symbol}：{e}")

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
                print(f"    [OK] {symbol}：{len(news_df)} 条新闻")
            else:
                print(f"    [WARN] {symbol}：无新闻")
        except Exception as e:
            print(f"    [WARN] {symbol} 新闻失败：{e}")

    if all_news:
        news_df = pd.concat(all_news, ignore_index=True).sort_values("datetime").reset_index(drop=True)
        print(f"\n  [OK] 总新闻：{len(news_df)} 条")
    else:
        news_df = pd.DataFrame(columns=["datetime", "title", "summary", "source", "sentiment_score", "symbol"])
        print(f"\n  [WARN] 无新闻数据")

    return market_data, news_df


# ==================== 路径 A：规则基线 ====================

def run_rule_baseline(market_data):
    """
    对每只候选股跑规则基线（MA/RSI/MACD 多条件投票 + 单股回测引擎）。
    返回 Dict[symbol, metrics_dict]
    """
    print("\n" + "=" * 60)
    print("  [2/4] 路径 A：规则基线（阶段 0/1）")
    print("  MA/RSI/MACD 多条件投票 → 单股回测")
    print("=" * 60)

    results = {}
    for symbol, df in market_data.items():
        print(f"\n  --- {symbol} ---")
        df_enriched = compute_all_indicators(df.copy())
        df_enriched = generate_signals(df_enriched)

        buy_count = (df_enriched["signal"] == config.SIGNAL_BUY).sum()
        sell_count = (df_enriched["signal"] == config.SIGNAL_SELL).sum()
        print(f"    信号：买入 {buy_count} 次，卖出 {sell_count} 次")

        engine = BacktestEngine()
        recorder = engine.run(df_enriched, symbol=symbol)
        metrics = recorder.evaluate()

        bh = recorder.evaluate_buy_and_hold(df)
        if bh:
            metrics.update(bh)

        results[symbol] = metrics
        print(f"    累计收益率：{metrics.get('累计收益率', 'N/A')}")

    return results


# ==================== 路径 B/C：多股回测（LLM / VADER）====================

def run_multi_stock_backtest(decide_func, market_data, news_df, candidate_pool, label=""):
    """
    用指定的决策函数跑多股票回测。
    返回 recorder
    """
    engine = MultiStockBacktestEngine(decide_func=decide_func)
    recorder = engine.run(market_data, news_df, candidate_pool)
    return recorder


def run_llm_and_vader(market_data, news_df, candidate_pool):
    """
    跑 LLM 路径和 VADER 路径，返回两者的 recorder。
    """
    print("\n" + "=" * 60)
    print("  [3/4] 路径 B + C：LLM 选股 / VADER 选股（阶段 2-3）")
    print("=" * 60)

    # --- 路径 B：LLM ---
    print(f"\n  --- 路径 B：LLM Top-K ---")

    def llm_decide(date, market_state, candidate_pool, news_df, current_holdings):
        return decide_formula_llm(date, market_state, candidate_pool, news_df, current_holdings)

    llm_recorder = run_multi_stock_backtest(
        llm_decide, market_data, news_df, candidate_pool, label="LLM"
    )
    print(f"  [OK] LLM 回测完成，最终权益：${llm_recorder.equity_curve[-1][1]:,.2f}"
          if llm_recorder.equity_curve else "  [OK] LLM 回测完成（无净值）")

    # --- 路径 C：VADER ---
    print(f"\n  --- 路径 C：VADER Top-K ---")

    def vader_decide(date, market_state, candidate_pool, news_df, current_holdings):
        return decide_formula_vader(date, market_state, candidate_pool, news_df, current_holdings)

    vader_recorder = run_multi_stock_backtest(
        vader_decide, market_data, news_df, candidate_pool, label="VADER"
    )
    print(f"  [OK] VADER 回测完成，最终权益：${vader_recorder.equity_curve[-1][1]:,.2f}"
          if vader_recorder.equity_curve else "  [OK] VADER 回测完成（无净值）")

    return llm_recorder, vader_recorder


# ==================== 对比表 ====================

def print_comparison_table(rule_results, llm_recorder, vader_recorder, market_data):
    """输出三条路径的统一对比表。"""
    print("\n" + "=" * 60)
    print("  [4/4] 对比表")
    print("=" * 60)

    # 收集各路径的绩效
    rows = []

    # 规则基线：取所有股票的平均
    if rule_results:
        rule_returns = []
        rule_drawdowns = []
        for sym, m in rule_results.items():
            ret_str = m.get("累计收益率", "0.00%").replace("%", "")
            dd_str = m.get("最大回撤", "0.00%").replace("%", "")
            try:
                rule_returns.append(float(ret_str))
                rule_drawdowns.append(float(dd_str))
            except ValueError:
                pass
        avg_ret = sum(rule_returns) / len(rule_returns) if rule_returns else 0
        avg_dd = sum(rule_drawdowns) / len(rule_drawdowns) if rule_drawdowns else 0
        rule_trades = sum(
            int(m.get("总交易次数", 0)) for m in rule_results.values()
        )
        rows.append({
            "路径": "规则基线",
            "累计收益率": f"{avg_ret:.2f}%",
            "最大回撤": f"{avg_dd:.2f}%",
            "交易次数": rule_trades,
        })

    # LLM 路径
    if llm_recorder and llm_recorder.equity_curve:
        llm_metrics = llm_recorder.evaluate()
        rows.append({
            "路径": "LLM Top-K",
            "累计收益率": llm_metrics.get("累计收益率", "N/A"),
            "最大回撤": llm_metrics.get("最大回撤", "N/A"),
            "交易次数": llm_metrics.get("总交易次数", 0),
        })

    # VADER 路径
    if vader_recorder and vader_recorder.equity_curve:
        vader_metrics = vader_recorder.evaluate()
        rows.append({
            "路径": "VADER Top-K",
            "累计收益率": vader_metrics.get("累计收益率", "N/A"),
            "最大回撤": vader_metrics.get("最大回撤", "N/A"),
            "交易次数": vader_metrics.get("总交易次数", 0),
        })

    # Buy & Hold 基准（第一只股票）
    first_sym = list(market_data.keys())[0]
    bh_df = market_data[first_sym]
    bh_ret = (bh_df.iloc[-1]["close"] - bh_df.iloc[0]["close"]) / bh_df.iloc[0]["close"]
    import numpy as np
    prices = bh_df["close"].values
    peak = np.maximum.accumulate(prices)
    bh_dd = ((peak - prices) / peak).max()
    rows.append({
        "路径": f"Buy&Hold ({first_sym})",
        "累计收益率": f"{bh_ret:.2%}",
        "最大回撤": f"{bh_dd:.2%}",
        "交易次数": "-",
    })

    # 打印表格
    if rows:
        df_table = pd.DataFrame(rows)
        print(f"\n{df_table.to_string(index=False)}")
    else:
        print("\n  无结果可对比")


# ==================== 保存 ====================

def save_all_results(rule_results, llm_recorder, vader_recorder, candidate_pool):
    """保存所有路径的结果到 output/ 目录。"""
    output_dir = config.OUTPUT_DIR / "e2e"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    saved = []

    # 规则基线
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

    if saved:
        print(f"\n  结果已保存到 {output_dir}：")
        for name in saved:
            print(f"    - {name}")


# ==================== 主函数 ====================

def parse_args():
    parser = argparse.ArgumentParser(description="阶段 1-3 端到端验证")
    parser.add_argument("--pool", nargs="+", default=["AAPL", "NVDA"],
                        help="候选股票池 (默认: AAPL NVDA)")
    parser.add_argument("--start", default="2026-07-07", help="起始日期")
    parser.add_argument("--end", default="2026-08-06", help="结束日期")
    parser.add_argument("--skip-rule", action="store_true", help="跳过规则基线")
    parser.add_argument("--skip-vader", action="store_true", help="跳过 VADER 路径")
    return parser.parse_args()


def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  阶段 1-3 端到端验证")
    print("  规则基线 / LLM Top-K / VADER Top-K")
    print("=" * 60)
    print(f"\n  候选池：{args.pool}")
    print(f"  时间：{args.start} ~ {args.end}")
    print(f"  初始资金：${config.INITIAL_CAPITAL:,.0f}")
    print(f"  Top-K：{config.TOP_K}  调仓周期：{config.REBALANCE_DAYS} 天")

    # 1. 数据采集（一次下载，三条路径共用）
    market_data, news_df = download_data(args.pool, args.start, args.end)

    # 2. 路径 A：规则基线
    rule_results = {}
    if not args.skip_rule:
        rule_results = run_rule_baseline(market_data)

    # 3. 路径 B + C：LLM / VADER
    llm_recorder, vader_recorder = run_llm_and_vader(
        market_data, news_df, args.pool
    )

    # 4. 对比表
    print_comparison_table(rule_results, llm_recorder, vader_recorder, market_data)

    # 5. 保存
    print("\n" + "=" * 60)
    print("  保存结果")
    print("=" * 60)
    save_all_results(rule_results, llm_recorder, vader_recorder, args.pool)

    print("\n" + "=" * 60)
    print("  [DONE] 阶段 1-3 端到端验证完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
