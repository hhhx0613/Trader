"""统一研究编排 / One stage-gated orchestration from collection to PortfolioIntent.

阶段门控集中在此：CLI、外部定时调度与观测工具共用同一函数，避免同串编排散落在
多个脚本里各写一遍（Plan.md 3.1.1「编排入口收敛」）。

三种节奏（``FlowOptions.mode``）：
  daily    采集 -> 建卡入池，不冻结。不碰模型，定时任务因此不需要 API key
  collect  采集 -> 建卡 -> 用本轮卡集点名冻结，一次得到一份可复现 Snapshot
  decide   按 ``available_from`` 从卡片池池选卡冻结 -> 逐标的研究图 -> Committee。
           全程不联网：读的都是每日 daily 已经入池的卡

三条边界：
  - 要决策就必须给真实账户。``decide`` 缺 ``model`` 或缺 ``account_state`` 直接
    ``ValueError``，不退回占位账户：Risk Critic 的 prompt 写明「缺 limits 或持仓
    状态不得当作放行」，占位账户只会烧出一轮全 ``abstain`` 的无效意图。
  - 观测不属于编排。逐节点打印是调用方对模型客户端的包装，本模块不含 print、
    事件总线或前端耦合。
  - 采集能力只在本模块这一侧。研究节点经 ``DataGateway`` 读冻结卡，拿不到任何
    采集入口，缺证据走显式 ``data_request`` 弃权，由下一轮 daily 补齐。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from core import config
from core.data import filings as filings_collector
from core.data import news as news_collector
from core.data.market import INDICATORS_VERSION, build_feature_card, ensure_market_coverage
from core.research import DataGateway, ResearchLedger
from core.research.contracts import EvidenceCard, PortfolioIntent, ResearchPacket, ResearchSnapshot
from core.research.raw import RawPayloadStore
from core.research.snapshot import SnapshotBuilder

from .committee import Committee
from .graph import PerAssetResearchGraph
from .types import JsonModel

MODES = ("daily", "collect", "decide")

# decide 所需账户字段：与 Risk Critic 的四项注入上下文同源，regime 可缺（只影响措辞、
# 不影响放行与否），但缺现金/持仓/限额时必须拒绝，否则 Critic 只能在空洞上签字。
_REQUIRED_ACCOUNT_KEYS = ("cash", "positions", "limits")


@dataclass(frozen=True)
class FlowOptions:
    """一轮编排的口径参数；除 ``as_of`` 外都不给隐式默认。"""

    pool: tuple[str, ...]
    mode: str = "daily"
    as_of: datetime | None = None          # None = 实盘：采集完成后才定时点
    news_days: int = 7
    market_lookback: int = 220
    filings: bool = True
    account_state: dict[str, Any] | None = None
    trace_prefix: str = "flow"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown flow mode: {self.mode}")
        normalized = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in self.pool))
        if not normalized or any(not symbol for symbol in normalized):
            raise ValueError("flow requires at least one non-empty symbol")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("flow as_of must be timezone-aware")
        object.__setattr__(self, "pool", normalized)


@dataclass(frozen=True)
class RoundResult:
    """本轮产出；daily 只有建卡数，decide 一路带到 PortfolioIntent。"""

    mode: str
    as_of: datetime
    ingested: int
    snapshot: ResearchSnapshot | None = None
    packets: tuple[ResearchPacket, ...] = ()
    intent: PortfolioIntent | None = None
    evidence: int = 0


def run_research_round(options: FlowOptions, *, model: JsonModel | None = None,
                       ledger: ResearchLedger | None = None,
                       raw_store: RawPayloadStore | None = None) -> RoundResult:
    """按 ``mode`` 执行到对应阶段为止；只做编排，不新增任何研究或采集逻辑。

    网络、落库、PIT 校验、去重全部委派给 ``core/data/*`` 与 ``core/research``。
    ``ledger``/``raw_store`` 供调用方（含离线测试与常驻调度器）复用已打开的连接，
    省略时按 ``config`` 的主线存储路径新建，并在返回前关掉自己打开的那个正文库。
    """
    if options.mode == "decide":
        _require_decision_inputs(model, options.account_state)

    book = ledger or ResearchLedger(config.RESEARCH_LEDGER_PATH)
    store = raw_store or RawPayloadStore(config.RESEARCH_RAW_DIR)
    try:
        return _round(options, model=model, book=book, store=store)
    finally:
        if raw_store is None:
            store.close()  # 自开的库自关；调用方交来的连接归调用方


def _round(options: FlowOptions, *, model: JsonModel | None, book: ResearchLedger,
           store: RawPayloadStore) -> RoundResult:
    """阶段门控本体；前置校验与连接生命周期已由 ``run_research_round`` 接手。"""
    builder = SnapshotBuilder(book, store)

    as_of = options.as_of
    cards: tuple[EvidenceCard, ...] = ()
    with _trace_guard(options.trace_prefix):
        if options.mode in ("daily", "collect"):
            # 先按当前时点算采集窗口，再在采集结束后重新定时点：实盘模式本轮拉到的
            # 数据本轮即可用；传了 --as-of 回放时时点钉死，晚到的数据被 PIT 闸门拒绝。
            window = as_of or datetime.now(timezone.utc)
            records = _collect(store, options, window)
            as_of = datetime.now(timezone.utc) if options.as_of is None else options.as_of
            for symbol in options.pool:
                feature = build_feature_card(symbol, as_of, lookback_days=options.market_lookback, store=store)
                if feature is not None:
                    records.append(feature)

        as_of = as_of or datetime.now(timezone.utc)
        stamp = as_of.strftime("%Y%m%dT%H%M%SZ")
        if options.mode in ("daily", "collect"):
            cards = tuple(builder.ingest_records(trace_id=f"{options.trace_prefix}-ingest-{stamp}",
                                                 as_of=as_of, records=records))

        if options.mode == "daily":
            return RoundResult(mode="daily", as_of=as_of, ingested=len(cards))

        source_versions = {"news": news_collector.NEWS_VERSION, "market": INDICATORS_VERSION,
                           "filings": filings_collector.FILINGS_VERSION}
        freeze_trace = f"{options.trace_prefix}-freeze-{stamp}"
        if options.mode == "collect":
            # collect 只验证数据链、不跑模型，缺账户时用明写的占位口径冻结（与 daily 同
            # 为「不产生决策」的节奏）；要拿到可执行意图请走 decide + 真实 account_state。
            snapshot = builder.freeze_snapshot(trace_id=freeze_trace, as_of=as_of, symbols=options.pool,
                                               source_versions=source_versions,
                                               account_state=options.account_state or {"as_of": as_of.isoformat(),
                                                                                       "note": "collect 不跑模型：占位账户"},
                                               evidence_ids=tuple(card.evidence_id for card in cards))
        else:
            # 决策日只冻结，不建卡：卡池由每日 daily 积累，本轮按可得窗口选卡。
            snapshot = builder.freeze_snapshot(trace_id=freeze_trace, as_of=as_of, symbols=options.pool,
                                               source_versions=source_versions,
                                               account_state=dict(options.account_state),
                                               available_from=as_of - timedelta(days=options.news_days))

        if model is None:
            return RoundResult(mode=options.mode, as_of=as_of, ingested=len(cards), snapshot=snapshot,
                               evidence=len(snapshot.evidence_ids))

        gateway = DataGateway(book, store)
        graph = PerAssetResearchGraph(ledger=book, gateway=gateway, model=model)
        packets = tuple(
            graph.run(snapshot_id=snapshot.snapshot_id, symbol=symbol, as_of=snapshot.as_of,
                      trace_id=f"{options.trace_prefix}-research-{stamp}-{symbol}")
            for symbol in options.pool)
        intent = Committee(ledger=book, gateway=gateway, model=model).run(
            snapshot_id=snapshot.snapshot_id, packet_ids=tuple(packet.packet_id for packet in packets),
            as_of=snapshot.as_of, trace_id=f"{options.trace_prefix}-committee-{stamp}")
    return RoundResult(mode=options.mode, as_of=as_of, ingested=len(cards), snapshot=snapshot,
                       packets=packets, intent=intent, evidence=len(snapshot.evidence_ids))


def _collect(store: RawPayloadStore, options: FlowOptions, window: datetime) -> list[dict[str, Any]]:
    """采集新闻与披露记录 + 保证行情素材批次已入库；K 线批次是素材不是事实，不进 Snapshot。"""
    end = window.strftime("%Y-%m-%d")
    news_start = (window - timedelta(days=options.news_days)).strftime("%Y-%m-%d")
    market_start = (window - timedelta(days=options.market_lookback)).strftime("%Y-%m-%d")
    records: list[dict[str, Any]] = []
    for symbol in options.pool:
        records.extend(news_collector.fetch_news_records(symbol, news_start, end, store=store))
        if options.filings:
            records.extend(filings_collector.fetch_filings_records(symbol, news_start, end))
        ensure_market_coverage(symbol, market_start, end, store=store)
    return records


def _require_decision_inputs(model: JsonModel | None, account_state: dict[str, Any] | None) -> None:
    """决策节奏的前置把关：没有模型跑不出意图，没有真实账户的意图不该花钱。"""
    if model is None:
        raise ValueError("decide requires an injected model; use mode=daily or mode=collect for data-only rounds")
    missing = [key for key in _REQUIRED_ACCOUNT_KEYS if key not in (account_state or {})]
    if missing:
        raise ValueError(f"decide requires account_state with real position data; missing {missing}")


@contextmanager
def _trace_guard(prefix: str) -> Iterator[None]:
    """把 trace 主键冲突翻成调度器能读懂的一句话。

    trace_id 取自 ``as_of`` 的秒级戳，同一时点重跑会撞主键。撞了是「这一轮已经记过账」，
    不是崩溃：让调度器按幂等信号处理，比在日志里留一段 sqlite 栈更容易归因。
    """
    try:
        yield
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"ledger already holds a trace for this as_of (prefix {prefix}); "
            f"rerun with a different --as-of instead of overwriting history"
        ) from exc
