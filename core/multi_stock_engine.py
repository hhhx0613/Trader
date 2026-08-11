"""
多股票回测引擎（MultiStockBacktestEngine）

Plan.md 阶段 3 对应：
  - 支持持有 K 只等权标的
  - 按调仓周期（每 N 个交易日）执行调仓
  - 信号时序：T-1 收盘生成信号 → T 开盘执行（防前视偏差）
  - 风控集成：risk_manager 硬规则（止损、回撤、禁止买入）
  - recorder 记录组合净值/交易

关键设计：
  - market_data: Dict[symbol, DataFrame]，每个 DataFrame 已包含 OHLCV + 技术指标
  - 持仓用 holdings dict 维护：{symbol: {"shares": int, "entry_price": float}}
  - 调仓日：每 REBALANCE_DAYS 个交易日触发一次决策
  - 非调仓日：持有不动，只记录净值
"""

import pandas as pd
from typing import Dict, List, Optional, Callable

from . import config
from .base_engine import BaseBacktestEngine
from .indicators import compute_all_indicators
from .recorder import Recorder


class MultiStockBacktestEngine(BaseBacktestEngine):
    """
    多股票回测引擎，支持 Top-K 等权组合 + 每周调仓。

    使用方法：
        engine = MultiStockBacktestEngine(decide_func=my_decide)
        recorder = engine.run(market_data, news_df, candidate_pool)
        recorder.print_report(primary_df)
    """

    def __init__(self, decide_func: Callable, initial_capital: float = None):
        """
        参数：
          decide_func: 决策函数回调
            签名：decide_func(date, market_state, candidate_pool, news_df, current_holdings)
                  -> {"holdings": [{"symbol": str, "weight": float}], "exposure": float}
          initial_capital: 初始资金（默认 config.INITIAL_CAPITAL）
        """
        super().__init__(initial_capital)
        self.decide_func = decide_func

        # 待执行决策（前一天生成，今天开盘执行）
        self._pending_decision = None

    def run(
        self,
        market_data: Dict[str, pd.DataFrame],
        news_df: pd.DataFrame,
        candidate_pool: List[str] = None,
    ) -> Recorder:
        """
        运行多股票回测。

        参数：
          market_data: Dict[symbol, DataFrame] 原始 OHLCV 数据
          news_df: 合并后的新闻 DataFrame（包含 symbol 列）
          candidate_pool: 候选股票池

        返回：
          Recorder 对象
        """
        # 重置状态
        self.reset()
        self._pending_decision = None

        if candidate_pool is None:
            candidate_pool = list(market_data.keys())

        # ========== 预处理：检查/计算技术指标 ==========
        print(f"\n[MultiStockEngine] 预处理技术指标...")
        enriched_data = {}
        for symbol, df in market_data.items():
            if "rsi" in df.columns and "adx" in df.columns and "ema_short" in df.columns:
                # 数据已含指标（由调用方预计算含预热期），直接使用
                enriched_data[symbol] = df
                print(f"  {symbol}: {len(df)} bars (预计算), "
                      f"RSI={df['rsi'].iloc[-1]:.1f}, "
                      f"MACD_hist={df['macd_hist'].iloc[-1]:.3f}")
            else:
                enriched_df = compute_all_indicators(df.copy())
                enriched_data[symbol] = enriched_df
                print(f"  {symbol}: {len(enriched_df)} bars, "
                      f"RSI={enriched_df['rsi'].iloc[-1]:.1f}, "
                      f"MACD_hist={enriched_df['macd_hist'].iloc[-1]:.3f}")

        # ========== 构建统一交易日历 ==========
        all_dates = set()
        for symbol, df in enriched_data.items():
            all_dates.update(df.index.tolist())
        all_dates = sorted(all_dates)

        # 空数据保护
        if not all_dates:
            print(f"\n[MultiStockEngine] 无行情数据，跳过回测")
            return self.recorder

        print(f"\n[MultiStockEngine] 开始回测...")
        print(f"  候选池：{candidate_pool}")
        print(f"  区间：{all_dates[0]} → {all_dates[-1]}")
        print(f"  总交易日：{len(all_dates)}")
        print(f"  调仓周期：每 {config.REBALANCE_DAYS} 天")
        print(f"  Top-K：{config.TOP_K}")
        print(f"  初始资金：${self.initial_capital:,.2f}")

        # ========== 逐日遍历 ==========
        rebalance_counter = 0

        for i, current_date in enumerate(all_dates):
            is_rebalance_day = (rebalance_counter % config.REBALANCE_DAYS == 0)

            # ========== 步骤 1：执行昨日待执行决策（以今日开盘价撮合）==========
            if self._pending_decision is not None:
                self._execute_rebalance(
                    current_date=current_date,
                    market_data=enriched_data,
                    decision=self._pending_decision,
                )
                self._pending_decision = None

            # ========== 步骤 2：风控检查（以收盘价计算浮盈亏）==========
            portfolio_value = self._calculate_portfolio_value(enriched_data, current_date)
            risk_status = self._check_risk(current_date, portfolio_value, enriched_data)

            # 风控强制平仓
            if risk_status.get("force_liquidate", False):
                self._liquidate_all(
                    current_date=current_date,
                    market_data=enriched_data,
                    reason=risk_status.get("halt_reason", "风控强制平仓"),
                )

            # ========== 步骤 3：生成今日决策（调仓日才生成）==========
            if is_rebalance_day:
                market_state = self._compute_market_state(enriched_data, current_date, candidate_pool)
                current_holding_symbols = set(self.holdings.keys())

                decision = self.decide_func(
                    date=current_date,
                    market_state=market_state,
                    candidate_pool=candidate_pool,
                    news_df=news_df,
                    current_holdings=current_holding_symbols,
                )

                # 风控禁止买入时，只允许减仓/清仓
                if not risk_status.get("allow_buy", True):
                    if decision.get("holdings"):
                        decision["holdings"] = [
                            h for h in decision["holdings"]
                            if h["symbol"] in current_holding_symbols
                        ]
                        if not decision["holdings"]:
                            decision["exposure"] = 0.0

                # 存入待执行（明天开盘执行）
                self._pending_decision = decision

            # 调仓计数器每天 +1（非仅调仓日），保证每 N 个交易日触发一次
            rebalance_counter += 1

            # ========== 步骤 4：记录今日净值 ==========
            portfolio_value = self._calculate_portfolio_value(enriched_data, current_date)
            self.recorder.log_equity(current_date, portfolio_value)

        # 回测结束
        final_value = self._calculate_portfolio_value(enriched_data, all_dates[-1])
        print(f"\n[MultiStockEngine] 回测完成。最终权益: ${final_value:,.2f}")

        return self.recorder

    def _compute_market_state(
        self,
        market_data: Dict[str, pd.DataFrame],
        current_date: pd.Timestamp,
        candidate_pool: List[str],
    ) -> Dict:
        """
        计算所有候选股的市场状态（技术指标），供决策函数使用。

        返回：
          {symbol: {"current_price": float, "rsi": float, "macd_hist": float, ...}}
        """
        market_state = {}

        for symbol in candidate_pool:
            if symbol not in market_data:
                continue

            df = market_data[symbol]
            if current_date not in df.index:
                continue

            bar = df.loc[current_date]

            # 近期收益率（regime 特征）
            loc = df.index.get_loc(current_date)
            window = min(20, loc + 1)
            past = df.iloc[max(0, loc - window + 1):loc + 1]
            rolling_20d = (past.iloc[-1]["close"] / past.iloc[0]["close"] - 1) * 100 if len(past) >= 5 else 0.0

            market_state[symbol] = {
                "current_price": float(bar["close"]),
                "rsi": float(bar.get("rsi", 50.0)),
                "macd_hist": float(bar.get("macd_hist", 0.0)),
                "adx": float(bar.get("adx", 25.0)),
                "atr": float(bar.get("atr", 0.0)),
                "ema_short": float(bar.get("ema_short", bar["close"])),
                "ema_long": float(bar.get("ema_long", bar["close"])),
                "rolling_20d": rolling_20d,
            }

        return market_state

    def _execute_rebalance(
        self,
        current_date: pd.Timestamp,
        market_data: Dict[str, pd.DataFrame],
        decision: Dict,
    ):
        """
        执行调仓（以开盘价撮合）。

        逻辑：
          1. 先卖出不在目标持仓中的股票
          2. 再买入/调整目标持仓的股票
        """
        target_holdings = decision.get("holdings", [])
        exposure = decision.get("exposure", 0.0)
        target_symbols = {h["symbol"] for h in target_holdings}

        # ===== 第 1 步：卖出不在目标中的持仓 =====
        symbols_to_sell = set(self.holdings.keys()) - target_symbols
        for symbol in symbols_to_sell:
            self._sell_all(symbol, current_date, market_data, reason="调仓卖出")

        # ===== 第 2 步：计算目标持仓的目标股数 =====
        portfolio_value = self._calculate_portfolio_value(market_data, current_date)
        target_equity = portfolio_value * exposure  # 总暴露对应的权益

        for h in target_holdings:
            symbol = h["symbol"]
            weight = h["weight"]
            target_value = target_equity * weight  # 这只股票的目标金额

            # 获取开盘价（执行价）
            buy_price = self._get_open_price(market_data, symbol, current_date)
            if buy_price is None or buy_price <= 0:
                continue

            # 目标股数（向下取整到整数股）
            target_shares = int(target_value / buy_price)

            # 当前股数
            current_shares = self.get_holding_shares(symbol)

            if target_shares > current_shares:
                # 需要加仓
                shares_to_buy = target_shares - current_shares
                self.buy(symbol, shares_to_buy, buy_price, current_date,
                         reason=f"调仓买入 weight={weight:.0%}")
            elif target_shares < current_shares:
                # 需要减仓
                shares_to_sell = current_shares - target_shares
                self.sell(symbol, shares_to_sell, buy_price, current_date,
                          reason=f"调仓减仓 weight={weight:.0%}")
            # else: 股数不变，不交易

    def _sell_all(self, symbol: str, date, market_data: Dict, reason: str = ""):
        """清仓某只股票。"""
        shares = self.get_holding_shares(symbol)
        if shares == 0:
            return

        price = self._get_close_price(market_data, symbol, date)
        if price is None:
            return

        self.sell(symbol, shares, price, date, reason)

    def _liquidate_all(self, current_date, market_data: Dict, reason: str = ""):
        """清仓所有持仓（风控触发）。"""
        symbols = list(self.holdings.keys())
        for symbol in symbols:
            self._sell_all(symbol, current_date, market_data, reason)

    def _check_risk(self, current_date, portfolio_value: float, market_data: Dict) -> Dict:
        """
        风控检查。

        返回：
          {"allow_buy": bool, "force_liquidate": bool, "halt_reason": str}
        """
        force_liquidate = False
        halt_reason = ""

        # 第 1 步：逐只检查单股止损
        for symbol, holding in list(self.holdings.items()):
            price = self._get_close_price(market_data, symbol, current_date)
            if price and holding["entry_price"] > 0:
                risk_status = self.risk_manager.check_risk(
                    current_date=current_date,
                    current_equity=portfolio_value,
                    holding_shares=holding["shares"],
                    entry_price=holding["entry_price"],
                    current_price=price,
                )

                if risk_status["force_sell"]:
                    force_liquidate = True
                    halt_reason = risk_status["halt_reason"]
                    break

        # 第 2 步：检查总回撤（只在未触发强制平仓时才检查）
        if not force_liquidate:
            risk_status = self.risk_manager.check_risk(
                current_date=current_date,
                current_equity=portfolio_value,
                holding_shares=0,
                entry_price=0.0,
                current_price=0.0,
            )
            allow_buy = risk_status["allow_buy"]
            if not allow_buy and not halt_reason:
                halt_reason = risk_status["halt_reason"]
        else:
            allow_buy = False

        return {
            "allow_buy": allow_buy,
            "force_liquidate": force_liquidate,
            "halt_reason": halt_reason,
        }

    def _get_open_price(self, market_data: Dict, symbol: str, date) -> Optional[float]:
        """获取某股票某天的开盘价。"""
        if symbol not in market_data or date not in market_data[symbol].index:
            return None
        return float(market_data[symbol].loc[date, "open"])

    def _get_close_price(self, market_data: Dict, symbol: str, date) -> Optional[float]:
        """获取某股票某天的收盘价。"""
        if symbol not in market_data or date not in market_data[symbol].index:
            return None
        return float(market_data[symbol].loc[date, "close"])

    def _calculate_portfolio_value(self, market_data: Dict, current_date) -> float:
        """计算组合总价值（现金 + 所有持仓市值）。"""
        total = self.cash
        for symbol, holding in self.holdings.items():
            price = self._get_close_price(market_data, symbol, current_date)
            if price:
                total += holding["shares"] * price
        return total
