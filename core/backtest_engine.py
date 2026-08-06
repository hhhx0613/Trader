"""
回测撮合引擎（BacktestEngine）

职责：
  单股票回测，逐 Bar 遍历历史行情，按 df.signal 列执行买卖，
  同时计算手续费、滑点，并记录完整的交易过程和净值变化。

为什么自研回测引擎而不用 Backtrader：
  1. 自研引擎对内部数据结构有完全控制，方便后续阶段接入 PPO（需要 Gym 接口）
  2. 代码透明，论文中可以详细说明撮合逻辑，增强可信度
  3. Backtrader 等框架虽然成熟但学习成本高，且过度封装不利于调试

关键设计（防止前视偏差 Look-Ahead Bias）：
  ┌──────────────────────────────────────────────────────────────┐
  │  时间线：  Day t-1 收盘 → 生成信号 → Day t 开盘 → 撮合成交  │
  │                                                              │
  │  - 信号基于 Day t-1 的收盘价和技术指标计算                    │
  │  - 成交以 Day t 的开盘价（加滑点）撮合                       │
  │  - 这样策略只能利用"已知"信息，符合真实交易时序              │
  └──────────────────────────────────────────────────────────────┘
  这是回测系统最容易出错的地方，必须严格保证信号不能用到未来数据。
"""

import pandas as pd

from . import config
from .base_engine import BaseBacktestEngine
from .recorder import Recorder


class BacktestEngine(BaseBacktestEngine):
    """
    单股票回测撮合引擎，逐 Bar 模拟交易过程。

    使用方法：
        engine = BacktestEngine()
        recorder = engine.run(df, symbol="AAPL")
        recorder.print_report(df)
    """

    def __init__(self, initial_capital: float = None):
        super().__init__(initial_capital)

        # 待执行信号（前一天生成，今天开盘执行）
        self._pending_signal = config.SIGNAL_HOLD
        self._pending_reason = ""

    def run(self, df: pd.DataFrame, symbol: str = "") -> Recorder:
        """
        运行回测，逐 Bar 遍历。

        参数：
          df: 包含 open/high/low/close/volume + 技术指标 + signal 列的 DataFrame
          symbol: 股票代码（用于交易记录，默认从 df.index.name 推断）

        返回：
          Recorder 对象（包含交易记录和净值曲线）

        执行流程（每个 Bar）：
          1. 执行昨日待执行信号（以今日开盘价撮合）
          2. 风控检查（以今日收盘价计算浮盈亏）
          3. 生成今日信号（以今日收盘价计算指标）→ 存入待执行队列
          4. 记录今日净值
        """
        # 重置状态
        self.reset()
        self._pending_signal = config.SIGNAL_HOLD
        current_symbol = symbol or df.index.name or "UNKNOWN"

        # 空数据保护
        if len(df) == 0:
            print(f"\n[BacktestEngine] 无行情数据，跳过回测：{current_symbol}")
            return self.recorder

        print(f"\n[BacktestEngine] 开始回测...")
        print(f"  标的: {current_symbol}")
        print(f"  区间: {df.index[0]} → {df.index[-1]}")
        print(f"  总Bar数: {len(df)}")
        print(f"  初始资金: ${self.initial_capital:,.2f}")

        for i in range(len(df)):
            current_bar = df.iloc[i]
            current_date = df.index[i]

            open_price = current_bar["open"]
            close_price = current_bar["close"]

            # ========== 获取今日信号（从 df.signal 列读取）==========
            signal = int(current_bar["signal"])
            signal_reason = "策略信号"

            # ========== 步骤 1：执行昨日待执行信号 ==========
            # 以今日开盘价 + 滑点 撮合成交
            if self._pending_signal == config.SIGNAL_BUY and self.get_holding_shares(current_symbol) == 0:
                # 计算最大可买股数（风控限制）
                exec_price = open_price * (1 + config.SLIPPAGE)
                equity = self.cash + self.get_holding_shares(current_symbol) * open_price
                max_shares = self.risk_manager.get_max_position_shares(equity, exec_price)
                available = int((self.cash - config.COMMISSION_PER_TRADE) / exec_price)
                buy_shares = min(available, max_shares)
                self.buy(current_symbol, buy_shares, open_price, current_date, self._pending_reason)

            elif self._pending_signal == config.SIGNAL_SELL and self.get_holding_shares(current_symbol) > 0:
                self.sell(
                    current_symbol,
                    self.get_holding_shares(current_symbol),
                    open_price,
                    current_date,
                    self._pending_reason,
                )

            # 将今日信号存入待执行队列（明天开盘执行）
            self._pending_signal = signal
            self._pending_reason = signal_reason

            # ========== 步骤 2：风控检查（以收盘价计算浮盈亏）==========
            holding_shares = self.get_holding_shares(current_symbol)
            current_equity = self.cash + holding_shares * close_price
            risk_status = self.risk_manager.check_risk(
                current_date=current_date,
                current_equity=current_equity,
                holding_shares=holding_shares,
                entry_price=self.get_holding_entry(current_symbol),
                current_price=close_price,
            )

            # 风控强制平仓：优先级高于策略信号
            if risk_status["force_sell"] and holding_shares > 0:
                # 清空待执行队列，直接以收盘价强制卖出
                self._pending_signal = config.SIGNAL_HOLD
                self.sell(current_symbol, holding_shares, close_price, current_date,
                          risk_status["halt_reason"])

            # 风控禁止买入：如果待执行的是买入信号，取消它
            if not risk_status["allow_buy"] and self._pending_signal == config.SIGNAL_BUY:
                self._pending_signal = config.SIGNAL_HOLD

            # ========== 步骤 3：记录今日净值 ==========
            equity = self.cash + self.get_holding_shares(current_symbol) * close_price
            self.recorder.log_equity(current_date, equity)

        # 回测结束
        final_equity = self.cash + self.get_holding_shares(current_symbol) * df.iloc[-1]["close"]
        print(f"[BacktestEngine] 回测完成。最终权益: ${final_equity:,.2f}")

        return self.recorder
