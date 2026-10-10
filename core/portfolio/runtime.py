"""从冻结的原始行情与显式账户配置构造 PortfolioPolicy 输入；缺失即拒绝。

政策参数集中读自根 ``config``；``risk_state`` 是运行时账户状态，只从 ``account`` 顶层取。
"""
from datetime import datetime, timedelta
import pandas as pd
import config
from core.data.market import MARKET_HISTORY_LOOKBACK_DAYS, PORTFOLIO_MIN_CLOSES, load_price_frame
from core.indicators import compute_all_indicators
from core.research.raw import RawPayloadStore
from .constraints import IntentConstraintBuilder
from .cost_gate import CostLimits, CostModel, TradingInputs
from .policy import AllocationConfig, VolatilityTarget
from .portfolio_policy import PortfolioPolicy
from .risk_projection import RiskLimits


def update_risk_state(account: dict, frames: dict[str, pd.DataFrame], *, drawdown_lock: float) -> dict:
    """纯状态转换：daily 与回测重放使用同一 ATR 止损、峰值和锁存规则。"""
    positions = {str(p["symbol"]).upper(): p for p in account.get("positions", ())}
    missing = sorted(set(positions) - set(frames))
    if missing:
        raise ValueError(f"missing PIT price frame for open positions: {missing}")
    prices = {s: float(frame["close"].iloc[-1]) for s, frame in frames.items()}
    equity = float(account["cash"]) + sum(float(p["quantity"]) * prices[s] for s, p in positions.items())
    if equity <= 0:
        raise ValueError("account equity must be positive for risk-state replay")
    previous = dict(account.get("risk_state") or {})
    peak = max(float(previous.get("peak_equity", equity)), equity)
    drawdown = 1.0 - equity / peak
    stops = []
    for symbol, position in positions.items():
        if symbol not in frames or float(position.get("quantity", 0)) <= 0:
            continue
        atr = float(compute_all_indicators(frames[symbol][["open", "high", "low", "close", "volume"]]).iloc[-1]["atr"])
        if float(position.get("avg_cost", 0)) > 0 and prices[symbol] < float(position["avg_cost"]) - config.ATR_STOP_MULTIPLIER * atr:
            stops.append(symbol)
    return {"peak_equity": peak, "drawdown": drawdown, "stop_loss": sorted(stops),
            "risk_locked": bool(previous.get("risk_locked", False) or drawdown >= drawdown_lock)}


def load_risk_state(account: dict, symbols: tuple[str, ...], as_of: datetime,
                    store: RawPayloadStore) -> dict:
    """从已入库的 PIT 日线重放当日风险状态；不联网，也不写入账户。"""
    held = tuple(str(position["symbol"]).upper() for position in account.get("positions", ()))
    monitored = tuple(dict.fromkeys((*symbols, *held)))
    start = (as_of - timedelta(days=MARKET_HISTORY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end = as_of.strftime("%Y-%m-%d")
    frames: dict[str, pd.DataFrame] = {}
    for symbol in monitored:
        frame = load_price_frame(symbol, start, end, store=store)
        frame = frame[frame["available_at"] <= as_of]
        # ATR(14) 以完整日线重放；数据不足时宁可不触发，也不能用半截 ATR。
        if len(frame) < 20:
            raise ValueError(f"insufficient PIT price history for risk state {symbol}: need 20 bars")
        frames[symbol] = frame
    return update_risk_state(account, frames, drawdown_lock=float(config.RISK_LIMITS["drawdown_lock"]))

def build_policy_and_inputs(
    account: dict,
    symbols: tuple[str, ...],
    as_of: datetime,
    store: RawPayloadStore,
):
    # 1) 账户运行时状态：risk_state 必须完整携带四个风控标记，缺任一项直接拒绝，
    #    防止用半残状态去驱动下游政策（drawdown/untradable/stop_loss/risk_locked）。
    # 2) 政策配置的完整性校验：每只标的都要在 SECTORS（行业归属）和 LIQUIDITY_CAPS（流动性上限）里登记，
    #    否则组合分配/风控无参可依，视为配置缺失而非默认放行。
    unmapped = [
        s for s in symbols
        if s not in config.SECTORS or s not in config.LIQUIDITY_CAPS
    ]
    if unmapped:
        raise ValueError(f"portfolio config missing sectors/liquidity_caps for {unmapped}")
    # 3) 读取采集阶段已准备的 PIT（point-in-time）400 个自然日行情窗口；
    #    再用 available_at<=as_of 过滤掉 as_of 之后才可得的记录，杜绝未来函数。
    #    每只标的至少需 253 条，留给后续 pct_change 一步差分后仍够 252 个交易日。
    frames = {}
    start = (as_of - timedelta(days=MARKET_HISTORY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end = as_of.strftime("%Y-%m-%d")
    for s in symbols:
        frame = load_price_frame(s, start, end, store=store)
        frame = frame[frame["available_at"] <= as_of]
        if len(frame) < PORTFOLIO_MIN_CLOSES:
            raise ValueError(
                f"insufficient PIT price history for {s}: need {PORTFOLIO_MIN_CLOSES} bars"
            )
        frames[s] = frame
    state = update_risk_state(
        account, frames,
        drawdown_lock=float(config.RISK_LIMITS["drawdown_lock"]),
    )
    # 4) 对齐收盘价面板：dropna 只保留所有标的都有价的交易日，取最近 252 天；
    #    对齐后不足 252 天说明样本不够稳健，拒绝计算协方差/波动率。
    closes = pd.DataFrame(
        {s: f["close"] for s, f in frames.items()}
    ).dropna().tail(252)
    if len(closes) < 252:
        raise ValueError("price histories lack 252 aligned PIT returns")
    # 5) 由对齐后的收盘价派生年化风险度量：日收益 -> 协方差(×252 年化) 与各标的年化波动率。
    returns = closes.pct_change().dropna()
    covariance = returns.cov() * 252
    volatilities = (returns.std() * 252**.5).to_dict()
    # 6) 账户持仓与市值：价格取每只 frame 的最后一个 PIT 收盘，账户价值=现金+持仓市值；
    #    账户价值非正无法做权重归一，直接拒绝。
    positions = {
        str(x["symbol"]).upper(): float(x["quantity"])
        for x in account["positions"]
    }
    prices = {s: float(frames[s]["close"].iloc[-1]) for s in symbols}
    value = float(account["cash"]) + sum(
        positions.get(s, 0) * prices[s] for s in symbols
    )
    if value <= 0:
        raise ValueError("account value must be positive")
    # 7) 当前权重：账户里没登记的标的面板补 0，保证字典覆盖全部 symbols，供政策做偏离度计算。
    current = {s: positions.get(s, 0) * prices[s] / value for s in symbols}
    # 8) 组装 PortfolioPolicy：所有政策参数一律来自根 config（单一权威来源），
    #    约束/分配/波动率目标/风控/成本门槛/成本模型逐项显式注入，不在这里塞默认值。
    allocation = AllocationConfig(
        **config.ALLOCATION,
        sectors=config.SECTORS,
        liquidity_caps=config.LIQUIDITY_CAPS,
    )
    policy = PortfolioPolicy(
        constraint_builder=IntentConstraintBuilder(
            max_position_weight=config.MAX_POSITION_WEIGHT,
            hold_band=config.HOLD_BAND,
            seed_weight=config.SEED_WEIGHT,
            max_holdings=config.MAX_HOLDINGS,
        ),
        allocation=allocation,
        volatility_target=VolatilityTarget(**config.VOLATILITY_TARGET),
        risk_limits=RiskLimits(**config.RISK_LIMITS, sectors=config.SECTORS),
        cost_limits=CostLimits(**config.COST_LIMITS),
        cost_model=CostModel(**config.COST_MODEL),
        policy_version=str(config.POLICY_VERSION),
    )
    # 9) 交易侧输入：现金/价格/当日成交量，以及 ADV（20 日均额）用于参与率与流动性约束；
    #    adv_notional 用「量×价」的近 20 日均值近似日均名义成交额。
    untradable = {
        s for s in symbols
        if prices[s] <= 0 or float(frames[s]["volume"].iloc[-1]) <= 0
    }
    trading = TradingInputs(
        account_value=value,
        cash=float(account["cash"]),
        prices=prices,
        volumes={s: float(frames[s]["volume"].iloc[-1]) for s in symbols},
        adv_notional={
            s: float((frames[s]["volume"] * frames[s]["close"]).tail(20).mean())
            for s in symbols
        },
        untradable_symbols=untradable,
    )
    # 返回政策对象 + 构造输入所需的一切：当前权重、波动率、协方差、交易输入、账户风控状态。
    return policy, current, volatilities, covariance.to_dict(), trading, state
