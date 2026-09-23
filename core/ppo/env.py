"""
PPO 训练环境（env）

一个「周频总仓位决策」的 gymnasium 环境：每周调仓日 PPO 观察 numeric(13)+llm(5)，
输出 5 档暴露 {0/25/50/75/100%}，环境按逆波动率权重配置组合、扣逐资产换手成本、给出差分夏普奖励。

口径对齐真实回测（见 Plan_v2 阶段 6A，消除 env→真实回测的迁移鸿沟）：
  - 个股权重：逆波动率加权（σ=ATR/close），对齐 decision_func._position_sizing
  - 决策频率：周频（每 step = 一个 ISO 周，周内逐日累乘收益）
  - 持仓子池：默认 config.TOP_K 只，对齐真实回测选股数
  - 市场特征：在【全池】上聚合（感知大盘 regime），与持仓子池分离——对齐 predictor 收 candidate_pool
  - 成本：按逐资产目标权重变化扣滑点、换手惩罚和固定佣金近似

数据增广（解决样本量的真正杠杆）：
  每个 episode 随机抽子池 + 随机起始周 + 随机周数 → 组合数爆炸，等效样本量远超固定路径。

阶段解耦：
  - 阶段一：llm_provider=None，llm 分支恒为 0，主干只学数值择时
  - 阶段二：传入 llm_provider，llm 分支填真实语义信号，喂给旁路
"""

from collections import deque
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import gymnasium as gym
from gymnasium import spaces

from core import config
from core.ppo.features import (
    NUMERIC_DIM, LLM_DIM, build_numeric_obs, zeros_llm, _LOOKBACK,
)

# 动作 → 总暴露档位（对齐 Plan_v2「离散 5 档 {0/25/50/75/100%}」）
EXPOSURE_LEVELS = np.array([0.0, 0.25, 0.50, 0.75, 1.00], dtype=np.float32)

# 单位成交额成本：滑点 + 换手惩罚；固定佣金按订单数折算到归一化账户。
_COST_RATE = config.SLIPPAGE + config.TURNOVER_PENALTY_RATIO


class DifferentialSharpe:
    """
    差分夏普比率奖励（Moody & Saffell, 2001, "Learning to Trade via Direct RL"）。

    为什么用它而非 α·return − β·drawdown − γ·turnover：
      1. 直接优化「风险调整后收益」，与 recorder 的夏普评估目标一致（训练=评估，不再错位）；
      2. 免去手调 α/β/γ——那三个系数极难平衡，是 RL 做交易最常见的翻车点；
      3. 在线递推收益的一阶矩 A、二阶矩 B，每步给出「本次收益对累积夏普的边际贡献」。

    递推（η 为自适应速率，越小越看重长期）：
      A_t = A_{t-1} + η·(R_t − A_{t-1})
      B_t = B_{t-1} + η·(R_t² − B_{t-1})
      ΔS_t = (B_{t-1}·ΔA_t − ½·A_{t-1}·ΔB_t) / (B_{t-1} − A_{t-1}²)^{3/2}
    """

    def __init__(self, eta: float = 0.01, reward_scale: float = 50.0):
        self.eta = eta
        self.scale = reward_scale   # ΔS 量级偏小，放大到 PPO 友好的 O(1)；需按实测标定
        self.reset()

    def reset(self):
        self.A = 0.0
        self.B = 1.0   # B 初始为正，保证方差 (B−A²)>0，首步分母不炸

    def __call__(self, r: float) -> float:
        r = float(r)
        A_prev, B_prev = self.A, self.B
        dA = self.eta * (r - A_prev)
        dB = self.eta * (r * r - B_prev)
        self.A = A_prev + dA
        self.B = B_prev + dB
        variance = max(B_prev - A_prev * A_prev, 1e-8)   # 方差下界，数值稳定
        dS = (B_prev * dA - 0.5 * A_prev * dB) / (variance ** 1.5)
        return float(np.clip(dS * self.scale, -10.0, 10.0))   # clip 防单步奖励爆炸


class PPOEnv(gym.Env):
    """周频总仓位决策环境（阶段一/二共用，靠 llm_provider 区分）。"""

    metadata = {"render_modes": []}

    def __init__(
        self,
        market_data: Dict[str, pd.DataFrame],
        symbol_pool: Optional[List[str]] = None,
        llm_provider: Optional[Callable] = None,
        subpool_size: int = config.TOP_K,   # 对齐真实回测选股数（默认 5）
        episode_min_days: int = 120,     # ≈6 个月（内部换算为周数）
        episode_max_days: int = 480,     # ≈2 年（内部换算为周数）
        reward_eta: float = 0.01,
        reward_scale: float = 50.0,
        seed: Optional[int] = None,
    ):
        """
        参数：
          market_data:    {symbol: DataFrame(含 compute_all_indicators 指标列)}
          symbol_pool:    可抽样的股票池；None 则用 market_data 全部
          llm_provider:   callable(date, symbols) -> llm_obs(5)；None=阶段一（填0）
          subpool_size:   每个 episode 随机抽取的子池大小（默认 TOP_K，对齐真实选股数）
          episode_min/max_days: episode 随机长度区间（天数，内部换算为周数）
        """
        super().__init__()
        self.market_data = market_data
        self.symbol_pool = list(symbol_pool) if symbol_pool else list(market_data.keys())
        self.llm_provider = llm_provider
        self.subpool_size = min(subpool_size, len(self.symbol_pool))
        self.episode_min_days = episode_min_days
        self.episode_max_days = episode_max_days
        self._rng = np.random.default_rng(seed)

        # 预计算「日收益面板」：所有股票 close 对齐到统一日历，缺失填 0
        # 为什么预计算：episode 随机切片时可直接按日期查收益，避免每步重复计算
        closes = pd.DataFrame({s: market_data[s]["close"] for s in self.symbol_pool
                               if s in market_data and "close" in market_data[s]}).sort_index()
        self.close_panel = closes.reindex(columns=self.symbol_pool)
        opens = pd.DataFrame({s: market_data[s]["open"] for s in self.symbol_pool
                              if s in market_data and "open" in market_data[s]})
        self.open_panel = opens.reindex(index=self.close_panel.index, columns=self.symbol_pool)
        self.return_panel = self.close_panel.pct_change().fillna(0.0)
        self._all_dates = self.return_panel.index

        # 预计算「个股波动率面板」σ=ATR/close，对齐 decision_func._stock_daily_vol；
        # 逆波动率加权要用。缺 atr 列 / 非正值统一回退 config.FALLBACK_VOLATILITY。
        vols = pd.DataFrame({s: market_data[s]["atr"] / market_data[s]["close"]
                             for s in self.symbol_pool
                             if s in market_data and "atr" in market_data[s]})
        vols = vols.reindex(columns=self.symbol_pool).reindex(self._all_dates)
        self.vol_panel = vols.where(vols > 0).fillna(config.FALLBACK_VOLATILITY)
        atrs = pd.DataFrame({s: market_data[s]["atr"] for s in self.symbol_pool
                             if s in market_data and "atr" in market_data[s]})
        self.atr_panel = atrs.reindex(columns=self.symbol_pool).reindex(self._all_dates).fillna(0.0)

        # 按 ISO 周分组交易日：每组首个交易日 = 调仓日（对齐 multi_stock_engine 周频调仓）
        self._weeks = self._group_by_iso_week(self._all_dates)
        self._min_start_week = self._first_valid_week(_LOOKBACK)   # 调仓日需留足特征窗口
        self._min_ep_weeks = max(1, episode_min_days // 5)         # 天数→周数近似换算
        self._max_ep_weeks = max(self._min_ep_weeks, episode_max_days // 5)
        if len(self._weeks) - 1 <= self._min_start_week + self._min_ep_weeks:
            raise ValueError(
                f"行情数据太短（{len(self._all_dates)} 天 / {len(self._weeks) - 1} 个可执行周周期），"
                f"不足以支撑 episode 随机化；请扩充历史数据（Plan_v2 建议 2015 至今）")

        # 观测/动作空间（两阶段结构一致，阶段一 llm 恒 0）
        self.observation_space = spaces.Dict({
            "numeric": spaces.Box(-np.inf, np.inf, shape=(NUMERIC_DIM,), dtype=np.float32),
            "llm": spaces.Box(-np.inf, np.inf, shape=(LLM_DIM,), dtype=np.float32),
        })
        self.action_space = spaces.Discrete(len(EXPOSURE_LEVELS))

        self._sharpe = DifferentialSharpe(eta=reward_eta, reward_scale=reward_scale)
        # episode 运行时状态（reset 中初始化）
        self._subpool: List[str] = []
        self._ep_start_week = 0
        self._i = 0
        self._prev_exposure = 0.0
        self._prev_target_weights = pd.Series(0.0, index=self.symbol_pool)
        self._entry_prices = pd.Series(np.nan, index=self.symbol_pool)
        self._equity = 1.0
        self._peak = 1.0
        self._trade_window = deque(maxlen=4)   # 近 4 周换手（≈近 20 交易日）

    # ---------- gymnasium 接口 ----------

    def reset(self, *, seed: Optional[int] = None, options=None):
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)

        # ① 随机子池（防过拟合特定股票；默认 TOP_K 只对齐真实选股数）
        self._subpool = list(self._rng.choice(
            self.symbol_pool, size=self.subpool_size, replace=False))

        # ② 随机起始周 + 随机周数（组合爆炸 → 等效样本量远超固定路径）
        n_decision_weeks = len(self._weeks) - 1
        latest_start = n_decision_weeks - self._min_ep_weeks
        self._ep_start_week = int(self._rng.integers(
            self._min_start_week, latest_start + 1))
        length = int(self._rng.integers(self._min_ep_weeks, self._max_ep_weeks + 1))
        self._ep_len = min(length, n_decision_weeks - self._ep_start_week)
        self._i = 0

        # ③ 重置账户与奖励状态
        self._prev_exposure = 0.0
        self._prev_target_weights = pd.Series(0.0, index=self.symbol_pool)
        self._entry_prices = pd.Series(np.nan, index=self.symbol_pool)
        self._equity = 1.0
        self._peak = 1.0
        self._trade_window = deque(maxlen=4)
        self._sharpe.reset()

        return self._get_obs(0.0, 0.0), {}

    def step(self, action):
        requested_exposure = float(EXPOSURE_LEVELS[int(action)])
        week_index = self._ep_start_week + self._i
        week_dates = self._weeks[week_index]
        rebalance_date = week_dates[0]

        # 个股权重：逆波动率加权（σ=ATR/close），对齐 decision_func._position_sizing。
        # 真实回测里选股由 LLM 定、权重由逆波动率定；训练 env 用随机子池 + 逆波动率，
        # 让 PPO 在与真实一致的「集中持仓 + 风险平价」波动结构下学总暴露择时。
        weights = self._inverse_vol_weights(rebalance_date)

        # R2 在调仓日收盘判定：只按此前真实持仓的 open→close 回撤限制本周新增仓位。
        r2_drawdown = self._day_drawdown(rebalance_date)
        allow_buy = r2_drawdown < config.MAX_DAILY_DRAWDOWN
        exposure = requested_exposure

        # 调仓日收盘后才有动作，订单下一交易日开盘成交：首日只计 open→close；
        # 随后持有到下一调仓日收盘。相邻动作的收益日互不重叠。
        target_weights = pd.Series(0.0, index=self.symbol_pool)
        target_weights.loc[self._subpool] = exposure * weights
        if not allow_buy:
            # R2 只禁新增或加仓，保留减仓自由；新标的维持 0。
            target_weights = target_weights.where(
                target_weights <= self._prev_target_weights, self._prev_target_weights)
            target_weights = target_weights.where(self._prev_target_weights > 0, 0.0)

        # 调仓成本：每个资产的目标权重变化都对应一笔买/卖；现金不交易。
        weight_changes = (target_weights - self._prev_target_weights).abs()
        turnover = float(weight_changes.sum())
        order_count = int((weight_changes > 1e-8).sum())
        commission = order_count * config.COMMISSION_PER_TRADE / config.INITIAL_CAPITAL
        rebalance_cost = turnover * _COST_RATE + commission

        # 订单在首个持有日开盘执行。加仓按开盘价更新加权入场价，清仓删除入场价。
        entry_date = week_dates[1]
        entry_opens = self.open_panel.loc[entry_date, self._subpool]
        for symbol in self._subpool:
            previous = float(self._prev_target_weights[symbol])
            target = float(target_weights[symbol])
            if target <= 1e-8:
                self._entry_prices[symbol] = np.nan
            elif target > previous + 1e-8:
                open_price = float(entry_opens[symbol])
                if previous > 1e-8 and pd.notna(self._entry_prices[symbol]):
                    self._entry_prices[symbol] = (
                        previous * self._entry_prices[symbol] + (target - previous) * open_price
                    ) / target
                else:
                    self._entry_prices[symbol] = open_price

        self._prev_target_weights = target_weights.copy()

        return_dates = week_dates[1:] + [self._weeks[week_index + 1][0]]
        week_equity = 1.0
        stop_symbols = []
        stop_cost = 0.0
        for day_index, d in enumerate(return_dates):
            if day_index == 0:
                entry_open = self.open_panel.loc[d, self._subpool]
                entry_close = self.close_panel.loc[d, self._subpool]
                if (entry_open.isna() | entry_close.isna() | (entry_open <= 0)).any():
                    raise ValueError(f"{d.date()} 缺少有效开盘/收盘价，无法按 T+1 开盘成交计算收益")
                daily_returns = entry_close / entry_open - 1.0
            else:
                daily_returns = self.return_panel.loc[d, self._subpool]
            day_ret = float((daily_returns * self._prev_target_weights.loc[self._subpool]).sum())
            week_equity *= (1.0 + day_ret)

            # 收盘风控：R1/R1a 只清触发标的，止损后的实际权重用于周内余下收益。
            stopped_today = self._stop_symbols(d, week_equity)
            for symbol in stopped_today:
                position_weight = float(self._prev_target_weights[symbol])
                self._prev_target_weights[symbol] = 0.0
                self._entry_prices[symbol] = np.nan
                stop_symbols.append(symbol)
                stop_cost += position_weight * config.SLIPPAGE
                stop_cost += config.COMMISSION_PER_TRADE / config.INITIAL_CAPITAL

        cost = rebalance_cost + stop_cost
        net_ret = (week_equity - 1.0) - cost

        # 更新净值 / 回撤
        # 路径依赖：equity/drawdown 累积本 episode 内此前所有动作的后果，并写进下一步 obs
        # （账户 3 维）→ 让 PPO「看到自己亏了多少」做路径依赖决策；每局 reset() 清零重来。
        self._equity *= (1.0 + net_ret)
        self._peak = max(self._peak, self._equity)
        drawdown = (self._peak - self._equity) / self._peak if self._peak > 0 else 0.0

        # 交易频率（近 4 周换手次数，≈近 20 交易日，对齐真实回测周频口径）
        self._trade_window.append(1 if turnover > 1e-6 else 0)
        recent_trades = int(sum(self._trade_window))

        reward = self._sharpe(net_ret)

        # 逆波动率归一化的浮点误差可能使 100% 暴露显示为 1+ε；账户特征保持合法区间。
        actual_exposure = float(np.clip(self._prev_target_weights.sum(), 0.0, 1.0))
        self._prev_exposure = actual_exposure
        self._i += 1
        truncated = self._i >= self._ep_len
        terminated = False   # 无自然终止，仅时间截断

        obs = self._get_obs(drawdown, actual_exposure, recent_trades)
        info = {"date": str(rebalance_date.date()), "net_ret": net_ret,
                "equity": self._equity, "exposure": actual_exposure,
                "requested_exposure": requested_exposure, "turnover": turnover,
                "cost": cost, "rebalance_cost": rebalance_cost, "stop_cost": stop_cost,
                "stop_symbols": stop_symbols, "r2_drawdown": r2_drawdown,
                "allow_buy": allow_buy, "week_ret": week_equity - 1.0}
        return obs, reward, terminated, truncated, info

    # ---------- 内部 ----------

    def _get_obs(self, drawdown: float, exposure: float, recent_trades: int = 0) -> Dict:
        """组装 Dict 观测：numeric(13) 走主干；llm(5) 走旁路（阶段一恒 0）。"""
        # 观测对齐「下一步将决策的调仓日」；episode 末尾 clamp 防越界。
        week_index = min(self._ep_start_week + self._i,
                         self._ep_start_week + self._ep_len - 1)
        date = self._weeks[week_index][0]
        account = {"current_exposure": exposure, "drawdown": drawdown,
                   "recent_trades": recent_trades}
        # 市场特征在【全池】上聚合（感知大盘 regime），持仓组合仍用【子池】（5 只逆波动率）。
        # 对齐真实回测：predictor 收 candidate_pool 全池算市场特征、只控制选出的 5 只暴露。
        numeric = build_numeric_obs(self.market_data, date, self.symbol_pool, account)
        llm = self.llm_provider(date, self._subpool) if self.llm_provider else zeros_llm()
        return {"numeric": numeric.astype(np.float32),
                "llm": np.asarray(llm, dtype=np.float32)}

    # ---------- 周频 / 逆波动率辅助 ----------

    @staticmethod
    def _group_by_iso_week(dates) -> List[List]:
        """把交易日按 ISO (年, 周) 分组；每组首个交易日即调仓日。
        对齐 multi_stock_engine：week_key=(iso_year, iso_week) 变化时触发调仓。"""
        weeks: List[List] = []
        prev_key = None
        for d in dates:
            iso = d.isocalendar()
            key = (iso[0], iso[1])
            if key != prev_key:
                weeks.append([])
                prev_key = key
            weeks[-1].append(d)
        return weeks

    def _first_valid_week(self, lookback: int) -> int:
        """首个「调仓日在全局日历 index ≥ lookback」的周索引，
        保证调仓日能取到完整 lookback 天特征窗口（否则 build_numeric_obs 越界）。"""
        for wi, week in enumerate(self._weeks):
            if self._all_dates.get_loc(week[0]) >= lookback:
                return wi
        return len(self._weeks) - 2

    def _inverse_vol_weights(self, date) -> pd.Series:
        """调仓日子池的逆波动率权重 w_i=(1/σ_i)/Σ(1/σ_j)，σ=ATR/close。
        对齐 decision_func._position_sizing 的个股权重口径。"""
        sigma = self.vol_panel.loc[date, self._subpool]
        inv = 1.0 / sigma
        return inv / inv.sum()

    def _day_drawdown(self, date) -> float:
        """已有持仓在调仓日 open→close 的组合回撤，供 R2 当日禁买使用。"""
        active = self._prev_target_weights.loc[self._subpool]
        if float(active.sum()) <= 1e-8:
            return 0.0
        opens = self.open_panel.loc[date, self._subpool]
        closes = self.close_panel.loc[date, self._subpool]
        if (opens.isna() | closes.isna() | (opens <= 0)).any():
            return 0.0
        return max(0.0, -float(((closes / opens - 1.0) * active).sum()))

    def _stop_symbols(self, date, week_equity: float) -> List[str]:
        """返回收盘触发 R1 或 ATR 止损的持仓；调用方负责逐只清仓。"""
        stopped = []
        for symbol in self._subpool:
            weight = float(self._prev_target_weights[symbol])
            entry = self._entry_prices[symbol]
            close = self.close_panel.loc[date, symbol]
            atr = self.atr_panel.loc[date, symbol]
            if weight <= 1e-8 or pd.isna(entry) or pd.isna(close):
                continue
            atr_stop = (
                config.USE_ATR_STOP and atr > 0
                and close < entry - config.ATR_STOP_MULTIPLIER * atr
            )
            single_loss = weight * (entry - close) / entry >= config.MAX_SINGLE_LOSS_RATIO
            if atr_stop or single_loss:
                stopped.append(symbol)
        return stopped
