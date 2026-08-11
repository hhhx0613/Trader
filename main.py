"""
主入口文件（main.py）

职责：
  串联阶段1的全部模块，执行完整的回测流程：
    数据采集 → 指标计算 → 信号生成 → 回测运行 → 报告输出 → 可视化

使用方法：
  python main.py                   # 默认回测 AAPL 2021-2024
  python main.py --symbol TSLA     # 回测特斯拉
  python main.py --start 2020-01-01 --end 2023-01-01  # 自定义时间范围

阶段1 完整流程：
  1. data_collector.fetch_ohlcv()   → 获取 K 线数据（三层兜底）
  2. indicators.compute_all_indicators() → 计算 EMA/RSI/MACD
  3. strategy.generate_signals()    → 生成买卖信号
  4. BacktestEngine.run()           → 逐 Bar 回测撮合
  5. Recorder.print_report()        → 输出绩效报告
  6. plot_results()                 → 绘制净值曲线 + 买卖点图
"""

import sys
import argparse
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")  # 非交互模式，避免在无 GUI 环境中阻塞
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# 从 core 模块导入
from core import config
from core.data.market_data import fetch_ohlcv
from core.indicators import compute_all_indicators
from core.strategy import generate_signals
from core.backtest_engine import BacktestEngine


# ==================== 主流程 ====================

def run_backtest(
    symbol: str = config.DEFAULT_SYMBOL,
    start_date: str = config.DEFAULT_START_DATE,
    end_date: str = config.DEFAULT_END_DATE,
):
    """
    执行一次完整回测，返回所有中间结果供可视化使用。

    返回：
      (df, recorder) 元组
      df       : 包含 OHLCV + 指标 + 信号的完整 DataFrame
      recorder : Recorder 对象（含交易记录和净值曲线）
    """
    # ---------- 第 1 步：获取数据 ----------
    print(f"\n{'='*60}")
    print(f"  量化交易 Agent 回测系统 — 阶段1 规则式策略")
    print(f"  标的: {symbol}  |  区间: {start_date} → {end_date}")
    print(f"{'='*60}\n")

    print("[Step 1/4] 获取行情数据...")
    df = fetch_ohlcv(symbol, start_date, end_date)
    if df is None or len(df) == 0:
        print(f"  ⚠️ 未获取到 {symbol} 的行情数据，请检查数据源或时间范围")
        return None, None
    print(f"  → 获取到 {len(df)} 根日K线")

    # ---------- 第 2 步：计算技术指标 ----------
    print("\n[Step 2/4] 计算技术指标 (EMA/RSI/MACD)...")
    df = compute_all_indicators(df)
    print(f"  → 已计算 EMA({config.EMA_SHORT}/{config.EMA_LONG}), "
          f"RSI({config.RSI_PERIOD}), MACD({config.MACD_FAST}/{config.MACD_SLOW}/{config.MACD_SIGNAL})")

    # ---------- 第 3 步：生成交易信号 ----------
    print("\n[Step 3/4] 生成交易信号...")
    df = generate_signals(df)
    buy_count = (df["signal"] == config.SIGNAL_BUY).sum()
    sell_count = (df["signal"] == config.SIGNAL_SELL).sum()
    print(f"  → 买入信号: {buy_count} 次, 卖出信号: {sell_count} 次")

    # ---------- 第 4 步：运行回测 ----------
    print("\n[Step 4/4] 运行回测撮合引擎...")
    engine = BacktestEngine()
    recorder = engine.run(df, symbol=symbol)

    # ---------- 输出报告 ----------
    recorder.print_report(df)

    # ---------- 保存结果 ----------
    print("\n[保存结果]")
    recorder.save_results(f"{symbol}_{start_date}_{end_date}")

    return df, recorder


# ==================== 可视化 ====================

def plot_results(df: pd.DataFrame, recorder, symbol: str = config.DEFAULT_SYMBOL):
    """
    绘制回测结果可视化图表：
      图1：股价走势 + 均线 + 买卖点标记
      图2：策略净值曲线 vs Buy & Hold 基准
      图3：回撤曲线
    """
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), gridspec_kw={"height_ratios": [2, 1.5, 1]})
    fig.suptitle(f"Backtest Report: {symbol}", fontsize=14, fontweight="bold")

    dates = df.index

    # ---------- 图1：股价 + 均线 + 买卖点 ----------
    ax1 = axes[0]
    ax1.plot(dates, df["close"], label="Close", color="gray", alpha=0.7, linewidth=1)
    ax1.plot(dates, df["ema_short"], label=f"EMA{config.EMA_SHORT}", color="blue", linewidth=1)
    ax1.plot(dates, df["ema_long"], label=f"EMA{config.EMA_LONG}", color="orange", linewidth=1)

    # 标记买卖点
    buy_signals = df[df["signal"] == config.SIGNAL_BUY]
    sell_signals = df[df["signal"] == config.SIGNAL_SELL]
    ax1.scatter(buy_signals.index, buy_signals["close"], marker="^", color="green",
                s=80, zorder=5, label="Buy")
    ax1.scatter(sell_signals.index, sell_signals["close"], marker="v", color="red",
                s=80, zorder=5, label="Sell")

    ax1.set_ylabel("Price ($)")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_title("Price & Signals")

    # ---------- 图2：净值曲线对比 ----------
    ax2 = axes[1]
    if recorder.equity_curve:
        eq_dates = [e[0] for e in recorder.equity_curve]
        eq_values = np.array([e[1] for e in recorder.equity_curve])
        # 归一化：初始净值 = 1.0
        eq_normalized = eq_values / eq_values[0]
        ax2.plot(eq_dates, eq_normalized, label="Strategy", color="blue", linewidth=1.5)

    # Buy & Hold 基准净值
    bh_normalized = df["close"] / df["close"].iloc[0]
    ax2.plot(dates, bh_normalized.values, label="Buy & Hold", color="gray",
             linewidth=1, linestyle="--")

    ax2.set_ylabel("Normalized Equity")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.set_title("Equity Curve (Strategy vs Buy & Hold)")

    # ---------- 图3：回撤曲线 ----------
    ax3 = axes[2]
    if recorder.equity_curve:
        peak = np.maximum.accumulate(eq_values)
        drawdown = (peak - eq_values) / peak * 100  # 百分比
        ax3.fill_between(eq_dates, 0, -drawdown, color="red", alpha=0.3)
        ax3.plot(eq_dates, -drawdown, color="red", linewidth=0.8)

    ax3.set_ylabel("Drawdown (%)")
    ax3.set_xlabel("Date")
    ax3.set_title("Drawdown")

    # 格式化 x 轴日期
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")

    plt.tight_layout()

    # 保存图表
    chart_path = config.OUTPUT_DIR / f"backtest_{symbol}.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    print(f"\n[可视化] 图表已保存: {chart_path}")
    plt.close(fig)  # 关闭图表释放内存


# ==================== 命令行入口 ====================

def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="量化交易 Agent 回测系统")
    parser.add_argument("--symbol", default=config.DEFAULT_SYMBOL, help="股票代码 (默认：AAPL)")
    parser.add_argument("--start", default=config.DEFAULT_START_DATE, help="起始日期 (默认：2021-01-01)")
    parser.add_argument("--end", default=config.DEFAULT_END_DATE, help="结束日期 (默认：2024-01-01)")
    return parser.parse_args()


def main():
    """主函数。"""
    args = parse_args()

    df, recorder = run_backtest(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
    )

    if df is not None and recorder is not None:
        plot_results(df, recorder, symbol=args.symbol)


if __name__ == "__main__":
    main()
