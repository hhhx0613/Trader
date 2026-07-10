"""
回测撮合引擎（BacktestEngine）

职责：
  模拟真实交易过程，逐 Bar 遍历历史行情，按策略信号执行买卖，
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
import numpy as np

import config
from risk_manager import RiskManager
from recorder import Recorder, Trade


class BacktestEngine:
    """
    回测撮合引擎，逐 Bar 模拟交易过程。

    使用方法：
        engine = BacktestEngine()
        engine.run(df)  # df 已包含 OHLCV + 技术指标 + 信号列
        engine.recorder.print_report(df)
    """

    def __init__(self):
        # 初始资金
        self.initial_capital = config.INITIAL_CAPITAL

        # 账户状态
        self.cash = self.initial_capital     # 可用现金
        self.shares = 0                       # 持仓股数
        self.entry_price = 0.0               # 持仓成本价（加权平均）

        # 子模块
        self.risk_manager = RiskManager(self.initial_capital)
        self.recorder = Recorder()

        # 待执行信号（前一天生成，今天开盘执行）
        self._pending_signal = config.SIGNAL_HOLD
        self._pending_reason = ""

    def run(self, df: pd.DataFrame) -> Recorder:
        """
        运行回测，逐 Bar 遍历。

        参数：
          df: 包含 open/high/low/close/volume + 技术指标 + signal 列的 DataFrame

        返回：
          Recorder 对象（包含交易记录和净值曲线）

        执行流程（每个 Bar）：
          1. 执行昨日待执行信号（以今日开盘价撮合）
          2. 风控检查（以今日收盘价计算浮盈亏）
          3. 生成今日信号（以今日收盘价计算指标）→ 存入待执行队列
          4. 记录今日净值
        """
        # 重置状态
        self.cash = self.initial_capital
        self.shares = 0
        self.entry_price = 0.0
        self.risk_manager.reset()
        self.recorder.reset()
        self._pending_signal = config.SIGNAL_HOLD

        print(f"\n[BacktestEngine] 开始回测...")
        print(f"  标的: {df.index.name or 'N/A'}")
        print(f"  区间: {df.index[0]} → {df.index[-1]}")
        print(f"  总Bar数: {len(df)}")
        print(f"  初始资金: ${self.initial_capital:,.2f}")

        for i in range(len(df)):
            current_bar = df.iloc[i]
            current_date = df.index[i]

            open_price = current_bar["open"]
            close_price = current_bar["close"]
            signal = int(current_bar["signal"])

            # ========== 步骤 1：执行昨日待执行信号 ==========
            # 以今日开盘价 + 滑点 撮合成交
            if self._pending_signal != config.SIGNAL_HOLD:
                self._execute_order(
                    self._pending_signal,
                    open_price,
                    current_date,
                    self._pending_reason,
                )

            # 将今日信号存入待执行队列（明天开盘执行）
            self._pending_signal = signal
            self._pending_reason = "策略信号"

            # ========== 步骤 2：风控检查（以收盘价计算浮盈亏）==========
            current_equity = self.cash + self.shares * close_price
            risk_status = self.risk_manager.check_risk(
                current_date=current_date,
                current_equity=current_equity,
                holding_shares=self.shares,
                entry_price=self.entry_price,
                current_price=close_price,
            )

            # 风控强制平仓：优先级高于策略信号
            if risk_status["force_sell"] and self.shares > 0:
                # 清空待执行队列，直接以收盘价强制卖出
                self._pending_signal = config.SIGNAL_HOLD
                self._execute_order(
                    config.SIGNAL_SELL,
                    close_price,
                    current_date,
                    risk_status["halt_reason"],  # 原因记录风控触发
                )

            # 风控禁止买入：如果待执行的是买入信号，取消它
            if not risk_status["allow_buy"] and self._pending_signal == config.SIGNAL_BUY:
                self._pending_signal = config.SIGNAL_HOLD

            # ========== 步骤 3：记录今日净值 ==========
            equity = self.cash + self.shares * close_price
            self.recorder.log_equity(current_date, equity)

        # 回测结束：如果还有持仓，以最后一天收盘价平仓（可选，这里保持持仓）
        print(f"[BacktestEngine] 回测完成。最终权益: ${self.cash + self.shares * df.iloc[-1]['close']:,.2f}")

        return self.recorder

    def _execute_order(
        self,
        signal: int,
        base_price: float,
        date,
        reason: str,
    ):
        """
        模拟撮合一笔订单。

        参数：
          signal     : SIGNAL_BUY 或 SIGNAL_SELL
          base_price : 基准价格（开盘价或风控时的收盘价）
          date       : 当前日期
          reason     : 交易原因

        撮合逻辑：
          - 买入：成交价 = base_price * (1 + slippage)  （滑点让买入更贵）
          - 卖出：成交价 = base_price * (1 - slippage)  （滑点让卖出更便宜）
          - 买入数量：用可用现金按最大仓位买入（向下取整到整数股）
          - 手续费：每笔固定金额（config.COMMISSION_PER_TRADE）
        """
        if signal == config.SIGNAL_BUY and self.shares == 0:
            # ---- 买入 ----
            # 滑点：实际买入价比基准价略高
            exec_price = base_price * (1 + config.SLIPPAGE)

            # 计算可买入的最大股数（考虑手续费）
            available_cash = self.cash - config.COMMISSION_PER_TRADE
            if available_cash <= 0 or exec_price <= 0:
                return

            # 风控限制：不超过最大仓位
            max_shares = self.risk_manager.get_max_position_shares(
                self.cash + self.shares * base_price, exec_price
            )
            buy_shares = min(int(available_cash / exec_price), max_shares)

            if buy_shares <= 0:
                return

            cost = buy_shares * exec_price + config.COMMISSION_PER_TRADE
            self.cash -= cost
            self.shares = buy_shares
            self.entry_price = exec_price

            # 记录交易
            self.recorder.log_trade(Trade(
                date=str(date),
                direction="BUY",
                price=exec_price,
                shares=buy_shares,
                commission=config.COMMISSION_PER_TRADE,
                pnl=0.0,  # 买入时无盈亏
                reason=reason,
            ))

        elif signal == config.SIGNAL_SELL and self.shares > 0:
            # ---- 卖出 ----
            # 滑点：实际卖出价比基准价略低
            exec_price = base_price * (1 - config.SLIPPAGE)

            # 先保存当前持仓信息（清零前）
            sell_shares = self.shares
            sell_entry = self.entry_price

            # 计算本笔盈亏
            revenue = sell_shares * exec_price - config.COMMISSION_PER_TRADE
            pnl = (exec_price - sell_entry) * sell_shares - config.COMMISSION_PER_TRADE

            self.cash += revenue
            self.shares = 0
            self.entry_price = 0.0

            self.recorder.log_trade(Trade(
                date=str(date),
                direction="SELL",
                price=exec_price,
                shares=sell_shares,
                commission=config.COMMISSION_PER_TRADE,
                pnl=pnl,
                reason=reason,
            ))
