"""跨模块稳定原因码 / Shared stable reason codes.

PIT 校验、研究否决、风险收缩和成本延后必须使用同一套字符串，不允许各层自造
同义异名代码。这里收录 `Plan.md` 已命名的全部原因码；新增或删除代码需先更新
`Plan.md`。成员是 str 子类，可直接用于既有 `PITValidationError(code, ...)` 与
JSON 序列化，`str(member)` 稳定输出小写下划线值。

Every layer -- PIT validation, research veto, risk shrinkage, cost deferral --
must reuse these codes instead of inventing synonyms. Membership changes require
a `Plan.md` update first. Members are str subclasses, so existing exception and
JSON paths keep emitting the lowercase snake_case value.
"""

from enum import Enum


class ReasonCode(str, Enum):
    # PIT 校验 / providers 与 Snapshot 构建阶段
    MISSING_AVAILABLE_AT = "missing_available_at"
    INVALID_AVAILABLE_AT = "invalid_available_at"
    TIMEZONE_REQUIRED = "timezone_required"
    FUTURE_DATA = "future_data"
    MISSING_IDENTITY = "missing_identity"
    INVALID_KIND = "invalid_kind"

    # 数据与账户可用性 / RiskProjection 第 1 步
    STALE_DATA = "stale_data"
    UNTRADABLE = "untradable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    # 模型与研究失败 / agents/research 只能落 abstain 或 data_request
    MODEL_FAILURE = "model_failure"
    SCHEMA_VIOLATION = "schema_violation"
    CITATION_OUT_OF_BOUNDS = "citation_out_of_bounds"
    UNRESOLVED_CONFLICT = "unresolved_conflict"

    # 风险硬限制 / RiskProjection 收缩与否决
    STOP_LOSS = "stop_loss"
    DRAWDOWN_LOCK = "drawdown_lock"
    RISK_STATE_LOCK = "risk_state_lock"
    CONCENTRATION_CAP = "concentration_cap"
    SECTOR_CAP = "sector_cap"
    EXPOSURE_CAP = "exposure_cap"
    CASH_FLOOR = "cash_floor"
    LIQUIDITY_CAP = "liquidity_cap"
    TURNOVER_CAP = "turnover_cap"

    # 成本与可执行性 / CostGate 延后或缩小
    MIN_ORDER_SIZE = "min_order_size"
    NO_TRADE_BAND = "no_trade_band"
    INSUFFICIENT_CASH = "insufficient_cash"
    COST_CAP = "cost_cap"
    DEFERRED_COST = "deferred_cost"
    DEFERRED_LIQUIDITY = "deferred_liquidity"

    def __str__(self) -> str:
        # f-string、日志与异常信息必须得到 "future_data" 而非 "ReasonCode.FUTURE_DATA"。
        return self.value
