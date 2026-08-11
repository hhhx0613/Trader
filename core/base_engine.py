"""
回测引擎基类（BaseBacktestEngine）

职责：
  提取 BacktestEngine 和 MultiStockBacktestEngine 的公共逻辑：
  - 资金管理（现金 + 持仓）
  - 买卖撮合（滑点 + 手续费 + 风控仓位限制）
  - 风控集成（RiskManager）
  - 净值记录（Recorder）
  - 交易结果反馈（R4 连亏降仓）

为什么抽取基类：
  两个引擎的买卖撮合逻辑完全一致（滑点方向、手续费扣除、持仓更新），
  之前各自维护一份，容易产生不一致。提取到基类后只需维护一处。
"""

from . import config
from .risk_manager import RiskManager
from .recorder import Recorder, Trade



class BaseBacktestEngine:
    """
    回测引擎基类，提供资金管理和交易撮合的公共实现。

    子类需实现：
      - run(): 回测主循环
    """

    def __init__(self, initial_capital: float = None):
        self.initial_capital = initial_capital or config.INITIAL_CAPITAL
        self.cash = self.initial_capital
        self.holdings: dict = {}  # {symbol: {"shares": int, "entry_price": float}}

        self.risk_manager = RiskManager(self.initial_capital)
        self.recorder = Recorder()

    def reset(self):
        """重置引擎状态（在新一轮回测开始前调用）。"""
        self.cash = self.initial_capital
        self.holdings = {}
        self.risk_manager.reset()
        self.recorder.reset()

    # ==================== 交易撮合 ====================

    def buy(self, symbol: str, shares: int, price: float, date, reason: str = ""):
        """
        买入股票（含滑点和手续费）。

        如果现金不足，自动减少买入量；如果减到 0 股则跳过。
        """
        if shares <= 0:
            return

        exec_price = price * (1 + config.SLIPPAGE)
        cost = shares * exec_price + config.COMMISSION_PER_TRADE

        if cost > self.cash:
            affordable = int((self.cash - config.COMMISSION_PER_TRADE) / exec_price)
            if affordable <= 0:
                return
            shares = affordable
            cost = shares * exec_price + config.COMMISSION_PER_TRADE

        self.cash -= cost

        # 更新持仓（加权平均成本）
        if symbol in self.holdings:
            old = self.holdings[symbol]
            total_shares = old["shares"] + shares
            avg_price = (old["shares"] * old["entry_price"] + shares * exec_price) / total_shares
            self.holdings[symbol] = {"shares": total_shares, "entry_price": avg_price}
        else:
            self.holdings[symbol] = {"shares": shares, "entry_price": exec_price}

        self.recorder.log_trade(Trade(
            date=str(date),
            symbol=symbol,
            direction="BUY",
            price=exec_price,
            shares=shares,
            commission=config.COMMISSION_PER_TRADE,
            pnl=0.0,
            reason=reason,
        ))

        # R5: 调仓交易记录换手并扣除惩罚
        if "调仓" in reason:
            self.risk_manager.record_turnover()
            turnover_cost = self.risk_manager.get_turnover_cost(shares * exec_price)
            self.cash -= turnover_cost

        print(f"  [BUY] {symbol} x{shares} @ ${exec_price:.2f} = ${shares * exec_price:,.2f} ({reason})")

    def sell(self, symbol: str, shares: int, price: float, date, reason: str = ""):
        """
        卖出股票（含滑点和手续费），并反馈风控。
        如果 shares 超过实际持仓，自动截断为持仓量。
        """
        if symbol not in self.holdings or shares <= 0:
            return

        holding = self.holdings[symbol]
        shares = min(shares, holding["shares"])

        exec_price = price * (1 - config.SLIPPAGE)
        revenue = shares * exec_price - config.COMMISSION_PER_TRADE
        pnl = (exec_price - holding["entry_price"]) * shares - config.COMMISSION_PER_TRADE

        self.cash += revenue

        # 更新持仓
        holding["shares"] -= shares
        if holding["shares"] <= 0:
            del self.holdings[symbol]

        self.recorder.log_trade(Trade(
            date=str(date),
            symbol=symbol,
            direction="SELL",
            price=exec_price,
            shares=shares,
            commission=config.COMMISSION_PER_TRADE,
            pnl=pnl,
            reason=reason,
        ))

        print(f"  [SELL] {symbol} x{shares} @ ${exec_price:.2f} = ${shares * exec_price:,.2f} "
              f"(pnl=${pnl:,.2f}, {reason})")

        # R4: 反馈交易结果给风控（更新连亏降仓状态）
        self.risk_manager.record_trade_result(pnl > 0)

        # R5: 调仓交易记录换手并扣除惩罚
        if "调仓" in reason:
            self.risk_manager.record_turnover()
            turnover_cost = self.risk_manager.get_turnover_cost(shares * exec_price)
            self.cash -= turnover_cost

    # ==================== 持仓查询 ====================

    def get_holding_shares(self, symbol: str) -> int:
        """获取某股票的持仓股数（未持有返回 0）。"""
        return self.holdings.get(symbol, {}).get("shares", 0)

    def get_holding_entry(self, symbol: str) -> float:
        """获取某股票的持仓成本价（未持有返回 0.0）。"""
        return self.holdings.get(symbol, {}).get("entry_price", 0.0)

    def calculate_portfolio_value(self, get_price_func) -> float:
        """
        计算组合总价值（现金 + 所有持仓市值）。

        参数：
          get_price_func: callable(symbol) -> Optional[float]
            返回某股票当前价格的函数，无法获取时返回 None
        """
        total = self.cash
        for symbol, holding in self.holdings.items():
            price = get_price_func(symbol)
            if price:
                total += holding["shares"] * price
        return total
