"""
交易记录与绩效评估模块（Recorder）

职责：
  1. 记录每一笔交易的详细信息（买卖方向、价格、数量、手续费等）
  2. 记录每日的账户净值（用于绘制净值曲线）
  3. 在回测结束后，基于交易记录和净值序列计算绩效评估指标

评估指标说明（论文中需要逐一解释）：
  - 累计收益率：策略整体盈亏比例，最直观的绩效指标
  - 最大回撤：从净值最高点到最低点的最大跌幅，衡量风险
  - 夏普比率：单位风险的超额收益，>1 为良好，>2 为优秀
  - 胜率：盈利交易占总交易的比例
  - 盈亏比：平均盈利 / 平均亏损，>1.5 为良好

为什么要有 Buy & Hold 基准：
  论文实验部分需要证明策略优于"什么都不做只买入"，
  因此 recorder 同时计算策略和基准的指标，方便对比。
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List
from pathlib import Path

from . import config


# ==================== 数据结构 ====================

@dataclass
class Trade:
    """单笔交易记录。"""
    date: str           # 交易日期
    symbol: str         # 股票代码（多股票回测时需要）
    direction: str      # "BUY" 或 "SELL"
    price: float        # 成交价格（已含滑点）
    shares: int         # 成交股数
    commission: float   # 手续费
    pnl: float          # 本笔盈亏（卖出时计算，买入时为 0）
    reason: str         # 交易原因（"策略信号" / "风控止损"）


# ==================== Recorder 类 ====================

class Recorder:
    """
    交易记录器，在回测过程中逐笔记录交易，回测结束后输出评估报告。
    """

    def __init__(self):
        # 交易记录列表
        self.trades: List[Trade] = []

        # 每日净值记录：[(date, equity), ...]
        self.equity_curve: List[tuple] = []

    def log_trade(self, trade: Trade):
        """记录一笔交易。"""
        self.trades.append(trade)

    def log_equity(self, date, equity: float):
        """记录当日账户总权益（现金 + 持仓市值）。"""
        self.equity_curve.append((date, equity))

    # ==================== 绩效评估 ====================

    def evaluate(self) -> dict:
        """
        计算全部绩效指标，返回指标字典。

        核心计算逻辑：
          1. 从 equity_curve 提取每日净值序列
          2. 计算日收益率序列
          3. 基于日收益率计算夏普比率、最大回撤等
        """
        if not self.equity_curve:
            return {"error": "无净值数据，无法评估"}

        # --- 提取净值序列 ---
        dates = [e[0] for e in self.equity_curve]
        equities = np.array([e[1] for e in self.equity_curve])

        initial = equities[0]
        final = equities[-1]

        # --- 日收益率序列 ---
        # daily_returns[t] = (equity[t] - equity[t-1]) / equity[t-1]
        daily_returns = np.diff(equities) / equities[:-1]

        # --- 1. 累计收益率 ---
        total_return = (final - initial) / initial

        # --- 2. 最大回撤 ---
        # 遍历净值序列，记录每个点到历史最高点的回撤
        peak = np.maximum.accumulate(equities)  # 历史最高净值序列
        drawdowns = (peak - equities) / peak   # 每个时间步的回撤比例
        max_drawdown = drawdowns.max()

        # --- 3. 夏普比率（年化）---
        # Sharpe = (年化收益率 - 无风险利率) / 年化波动率
        # 年化收益率 = 日均收益 * 252
        # 年化波动率 = 日波动率 * sqrt(252)
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            annual_return = daily_returns.mean() * 252
            annual_volatility = daily_returns.std() * np.sqrt(252)
            sharpe_ratio = (annual_return - config.RISK_FREE_RATE) / annual_volatility
        else:
            sharpe_ratio = 0.0

        # --- 4. 胜率和盈亏比 ---
        # 只统计已平仓的交易（有 pnl 值的卖出交易）
        pnl_list = [t.pnl for t in self.trades if t.direction == "SELL" and t.pnl != 0]

        if pnl_list:
            wins = [p for p in pnl_list if p > 0]
            losses = [p for p in pnl_list if p <= 0]
            win_rate = len(wins) / len(pnl_list) if pnl_list else 0
            avg_win = np.mean(wins) if wins else 0
            avg_loss = abs(np.mean(losses)) if losses else 1  # 防止除以 0
            profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
        else:
            win_rate = 0
            profit_loss_ratio = 0
            avg_win = 0
            avg_loss = 0

        # --- 5. 交易统计 ---
        total_trades = len([t for t in self.trades if t.direction == "BUY"])
        total_commission = sum(t.commission for t in self.trades)

        metrics = {
            "初始资金": f"${initial:,.2f}",
            "最终权益": f"${final:,.2f}",
            "累计收益率": f"{total_return:.2%}",
            "最大回撤": f"{max_drawdown:.2%}",
            "夏普比率": f"{sharpe_ratio:.2f}",
            "年化收益率": f"{(daily_returns.mean() * 252) if len(daily_returns) > 0 else 0:.2%}",
            "年化波动率": f"{(daily_returns.std() * np.sqrt(252)) if len(daily_returns) > 1 else 0:.2%}",
            "总交易次数": total_trades,
            "胜率": f"{win_rate:.2%}",
            "盈亏比": f"{profit_loss_ratio:.2f}",
            "平均盈利": f"${avg_win:,.2f}",
            "平均亏损": f"${avg_loss:,.2f}",
            "总手续费": f"${total_commission:,.2f}",
        }

        return metrics

    def evaluate_buy_and_hold(self, df: pd.DataFrame) -> dict:
        """
        计算 Buy & Hold（买入持有）基准策略的绩效。

        假设在第一天用全部资金买入，持有到最后一天不卖。
        这个基准用于论文实验对比：策略必须跑赢 Buy & Hold 才有价值。

        参数：
          df: 包含 close 列的 DataFrame（原始行情数据）
        """
        if df.empty:
            return {}

        initial_price = df.iloc[0]["close"]
        final_price = df.iloc[-1]["close"]
        total_return = (final_price - initial_price) / initial_price

        # Buy & Hold 的日收益率 = 股价日收益率
        daily_returns = df["close"].pct_change().dropna().values

        if len(daily_returns) > 1 and daily_returns.std() > 0:
            annual_return = daily_returns.mean() * 252
            annual_volatility = daily_returns.std() * np.sqrt(252)
            sharpe_ratio = (annual_return - config.RISK_FREE_RATE) / annual_volatility
        else:
            sharpe_ratio = 0.0

        # 最大回撤
        prices = df["close"].values
        peak = np.maximum.accumulate(prices)
        drawdowns = (peak - prices) / peak
        max_drawdown = drawdowns.max()

        return {
            "基准(Buy&Hold)累计收益率": f"{total_return:.2%}",
            "基准(Buy&Hold)最大回撤": f"{max_drawdown:.2%}",
            "基准(Buy&Hold)夏普比率": f"{sharpe_ratio:.2f}",
        }

    # ==================== 报告输出 ====================

    def print_report(self, df: pd.DataFrame):
        """打印完整的回测报告（策略 + 基准对比）。"""
        print("\n" + "=" * 60)
        print("             回 测 绩 效 报 告")
        print("=" * 60)

        # 策略绩效
        metrics = self.evaluate()
        print("\n【策略绩效】")
        for key, value in metrics.items():
            print(f"  {key:<16}: {value}")

        # 基准绩效
        bh_metrics = self.evaluate_buy_and_hold(df)
        if bh_metrics:
            print("\n【基准对比 (Buy & Hold)】")
            for key, value in bh_metrics.items():
                print(f"  {key:<28}: {value}")

        print("\n" + "=" * 60)

    def save_results(self, filename_prefix: str = "backtest"):
        """
        将交易记录和净值曲线保存为 CSV 文件。

        输出到 config.OUTPUT_DIR 目录：
          - {prefix}_trades.csv   : 交易明细
          - {prefix}_equity.csv   : 每日净值
          - {prefix}_metrics.csv  : 绩效指标汇总
        """
        output_dir = config.OUTPUT_DIR

        # 保存交易记录
        if self.trades:
            trade_records = [
                {
                    "日期": t.date,
                    "方向": t.direction,
                    "价格": t.price,
                    "股数": t.shares,
                    "手续费": t.commission,
                    "盈亏": t.pnl,
                    "原因": t.reason,
                }
                for t in self.trades
            ]
            pd.DataFrame(trade_records).to_csv(
                output_dir / f"{filename_prefix}_trades.csv",
                index=False,
                encoding="utf-8-sig",  # Excel 兼容的中文编码
            )
            print(f"[Recorder] 交易记录已保存: {filename_prefix}_trades.csv")

        # 保存净值曲线
        if self.equity_curve:
            equity_df = pd.DataFrame(
                self.equity_curve, columns=["date", "equity"]
            )
            equity_df.to_csv(
                output_dir / f"{filename_prefix}_equity.csv",
                index=False,
            )
            print(f"[Recorder] 净值曲线已保存: {filename_prefix}_equity.csv")

        # 保存绩效指标
        metrics = self.evaluate()
        pd.DataFrame([metrics]).to_csv(
            output_dir / f"{filename_prefix}_metrics.csv",
            index=False,
            encoding="utf-8-sig",
        )
        print(f"[Recorder] 绩效指标已保存: {filename_prefix}_metrics.csv")

    def reset(self):
        """清空所有记录（用于多次回测实验）。"""
        self.trades.clear()
        self.equity_curve.clear()
