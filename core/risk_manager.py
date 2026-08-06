"""
风控模块（RiskManager）

职责：
  对策略信号进行安全审核，在风险超标时拦截交易或强制平仓。
  风控是独立于策略之外的"硬护栏"——无论策略多看好，风控都有最终否决权。

阶段1 实现的风控规则（极简版，阶段4会扩展为独立风控Agent）：
  R1: 单笔止损 —— 持仓浮亏超过总资金 2% → 强制卖出
  R2: 单日最大回撤 —— 当日账户回撤超过 5% → 禁止新开仓
  R3: 最大仓位限制 —— 持仓不超过总资金的 100%
  R4: 连亏降仓 —— 连续亏损 N 笔后降低仓位比例
  R5: 换手惩罚 —— 每次调仓交易扣除一定比例作为惩罚

为什么风控要独立于策略：
  - 策略的目标是追求收益，天然有"冒险倾向"
  - 风控的目标是控制风险，必须站在策略的对立面做终审
  - 这种「策略 + 风控」双轨架构是本项目的核心安全设计，
    阶段2的 PPO 和阶段4的多Agent都会沿用这个设计
"""

from . import config


class RiskManager:
    """
    风控管理器，逐 Bar 检查账户状态并决定是否允许交易。

    生命周期：
      1. 回测引擎在每个时间步开始时调用 check_risk()
      2. check_risk() 返回当前风控状态
      3. 回测引擎根据风控状态决定是否执行策略信号
    """

    def __init__(self, initial_capital: float):
        """
        参数：
          initial_capital: 初始资金，用于计算回撤百分比
        """
        self.initial_capital = initial_capital

        # 记录历史最高净值（用于计算最大回撤）
        self.peak_equity = initial_capital

        # 当日是否被禁止交易（日内回撤超限后触发）
        self.daily_trading_halted = False

        # 记录上一个交易日，用于在新一天重置日内限制
        self.last_date = None

        # R4: 连亏降仓状态
        self.consecutive_losses = 0       # 当前连续亏损笔数
        self.position_multiplier = 1.0    # 仓位乘数（连亏触发后降低）

        # R5: 换手惩罚累计（记录当期调仓交易笔数）
        self.turnover_count = 0

    def check_risk(
        self,
        current_date,
        current_equity: float,
        holding_shares: int,
        entry_price: float,
        current_price: float,
    ) -> dict:
        """
        在每个时间步调用，检查所有风控规则。

        参数：
          current_date    : 当前日期（用于日内限制重置）
          current_equity  : 当前账户总权益（现金 + 持仓市值）
          holding_shares  : 当前持仓股数（0 = 空仓）
          entry_price     : 持仓成本价（空仓时为 0）
          current_price   : 当前股价

        返回：
          dict，包含风控决策信息：
            allow_buy      : bool，是否允许买入
            force_sell     : bool，是否强制平仓（止损触发）
            halt_reason    : str，禁止交易的原因（空字符串 = 无限制）
        """
        # 新的一天重置日内交易限制
        if current_date != self.last_date:
            self.daily_trading_halted = False
            self.last_date = current_date

        # 更新历史最高净值
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        allow_buy = True
        force_sell = False
        halt_reason = ""

        # ---------- R1: 单笔止损 ----------
        # 持仓浮亏占总资金比例超过阈值 → 强制平仓
        if holding_shares > 0 and entry_price > 0:
            unrealized_loss = (entry_price - current_price) * holding_shares
            loss_ratio = unrealized_loss / self.initial_capital

            if loss_ratio >= config.MAX_SINGLE_LOSS_RATIO:
                force_sell = True
                halt_reason = (
                    f"止损触发: 浮亏 {loss_ratio:.1%} "
                    f"超过阈值 {config.MAX_SINGLE_LOSS_RATIO:.1%}"
                )

        # ---------- R2: 单日最大回撤 ----------
        # 当日账户从最高点到当前的回撤超过阈值 → 禁止新开仓
        if self.peak_equity > 0:
            daily_drawdown = (self.peak_equity - current_equity) / self.peak_equity

            if daily_drawdown >= config.MAX_DAILY_DRAWDOWN:
                allow_buy = False
                self.daily_trading_halted = True
                if not halt_reason:
                    halt_reason = (
                        f"日内回撤 {daily_drawdown:.1%} "
                        f"超过阈值 {config.MAX_DAILY_DRAWDOWN:.1%}"
                    )

        # 日内已被禁止交易
        if self.daily_trading_halted:
            allow_buy = False
            if not halt_reason:
                halt_reason = "日内交易已被暂停"

        return {
            "allow_buy": allow_buy,
            "force_sell": force_sell,
            "halt_reason": halt_reason,
            "position_multiplier": self.position_multiplier,  # R4: 仓位乘数
        }

    def record_trade_result(self, is_profit: bool):
        """
        R4: 记录交易结果，更新连亏降仓状态。

        参数：
          is_profit: 本笔交易是否盈利
        """
        if is_profit:
            self.consecutive_losses = 0
            self.position_multiplier = 1.0  # 盈利后恢复
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
                self.position_multiplier = config.CONSECUTIVE_LOSS_PENALTY

    def record_turnover(self):
        """
        R5: 记录一次调仓交易（用于换手惩罚计算）。
        """
        self.turnover_count += 1

    def get_turnover_cost(self, target_value: float) -> float:
        """
        R5: 计算换手惩罚成本。

        参数：
          target_value: 本次调仓的目标交易金额

        返回：
          惩罚成本（美元）
        """
        return target_value * config.TURNOVER_PENALTY_RATIO

    def get_max_position_shares(
        self, current_equity: float, current_price: float
    ) -> int:
        """
        计算当前允许的最大持仓股数（考虑连亏降仓乘数）。

        参数：
          current_equity: 当前账户总权益
          current_price : 当前股价

        返回：
          最大可持有股数（整数，向下取整）
        """
        if current_price <= 0:
            return 0

        max_value = current_equity * config.MAX_POSITION_RATIO * self.position_multiplier
        return int(max_value / current_price)

    def reset(self):
        """重置风控状态（在新一轮回测开始前调用）。"""
        self.peak_equity = self.initial_capital
        self.daily_trading_halted = False
        self.last_date = None
        self.consecutive_losses = 0
        self.position_multiplier = 1.0
        self.turnover_count = 0
