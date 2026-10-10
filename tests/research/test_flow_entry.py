"""统一编排入口的离线回归 / Offline regression for the stage-gated flow entry.

只覆盖 flow 自己负责的三件事，链路内部行为已由 stage0-3 回归守住：
  1. 模式门控：daily 建卡入池且不碰模型、不冻结；collect 用本轮卡集点名冻结
  2. decide 的前置把关：缺模型或缺真实账户直接拒绝；冻结走卡片池按窗口选卡，
     一路跑到 PortfolioIntent
  3. 同一 as_of 重跑时 trace 主键冲突翻成可读 ValueError，而不是 sqlite 栈

全部离线：采集器被替换成 fixture 记录，模型是无网络 fake，不触任何真实存储。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agents.research.flow as flow
from agents.research.flow import FlowOptions, run_research_round
from core.research import ResearchLedger
from core.research.raw import RawPayloadStore
from core.research.snapshot import SnapshotBuilder
from core.portfolio import AllocationConfig, CostLimits, CostModel, IntentConstraintBuilder, PortfolioPolicy, RiskLimits, TradingInputs, VolatilityTarget

AS_OF = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)

# 决策节奏要求的真实账户字段；缺 limits/positions 时 flow 必须拒绝而不是退回占位
ACCOUNT = {"cash": 50_000.0, "positions": [{"symbol": "AAPL", "quantity": 100, "avg_cost": 210.0}],
           "limits": {"max_concentration": 0.35}, "regime": "risk-on"}

_CLAIM_ID = re.compile(r"'claim_id': '([^']+)'")


def news_record(**updates):
    record = {"kind": "news", "symbol": "AAPL", "source": "fixture", "title": "Earnings beat",
              "summary": "Revenue increased", "url": "https://example.test/a",
              "published_at": AS_OF - timedelta(hours=2), "available_at": AS_OF - timedelta(hours=1)}
    record.update(updates)
    return record


def market_record(**updates):
    record = {"kind": "market", "type": "feature", "symbol": "AAPL", "source": "program:fixture",
              "title": "AAPL market features", "summary": "", "body": json.dumps({"rsi": 61.0, "trend": 1}),
              "url": None, "published_at": AS_OF - timedelta(hours=3), "available_at": AS_OF - timedelta(hours=3)}
    record.update(updates)
    return record


class BoomModel:
    """任何模型调用都说明编排门控出错：数据节奏不该拉起 LLM。"""

    def chat_json(self, *_args, **_kwargs):
        raise AssertionError("daily/collect must not reach the model")

    def chat_with_tools(self, *_args, **_kwargs):
        raise AssertionError("daily/collect must not reach the model")


class FlowModel:
    """无网络 fake：Agent 经 chat_with_tools 补查真实卡 ID，Critic 与 Committee 走 chat_json。"""

    def chat_with_tools(self, message, *, tool_executor, **_kwargs):
        if message.startswith("{'agent': 'market'"):
            hits = json.loads(tool_executor("search_evidence", {"query": "features"}))
            statement = "Trend confirms the move"
        elif message.startswith("{'agent': 'fundamental'"):
            hits = json.loads(tool_executor("search_evidence", {"query": "revenue"}))
            statement = "Filing corroborates revenue"
        else:
            hits = json.loads(tool_executor("search_evidence", {"query": "earnings"}))
            statement = "Demand beat expectations"
        if not hits:  # 本轮没有该维度证据：不立论，交给 abstain 路由
            return {"claims": []}
        return {"claims": [{"statement": statement, "stance": "bullish", "confidence": 0.7,
                            "supporting_evidence_ids": [hits[0]["evidence_id"]]}]}

    def chat_json(self, message, **_kwargs):
        if "'thesis_book'" in message:
            cited = _CLAIM_ID.findall(message)
            return {"items": [{"symbol": "AAPL", "action": "long", "strength": 2, "priority": 1,
                               "horizon_days": 20, "supporting_card_ids": cited[:1],
                               "opposing_card_ids": [], "rationale": "cited claim survives critic"}]}
        return {"verdict": "allow", "reasons": ["cited claims are consistent"], "card_ids": []}


@pytest.fixture
def stores(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.db")
    return ledger, RawPayloadStore(tmp_path / "raw")


def _stub_collectors(monkeypatch, records):
    """把 flow 的四个采集触点换成 fixture 记录；口径字段保留，source_versions 仍要读。"""
    monkeypatch.setattr(flow, "news_collector", SimpleNamespace(
        NEWS_VERSION="news-v1", fetch_news_records=lambda symbol, start, end, *, store=None:
        [item for item in records if item["kind"] == "news" and item["symbol"] == symbol]))
    monkeypatch.setattr(flow, "filings_collector", SimpleNamespace(
        FILINGS_VERSION="filings-v1", fetch_filings_records=lambda symbol, start, end:
        [item for item in records if item["kind"] == "filing" and item["symbol"] == symbol]))
    monkeypatch.setattr(flow, "prepare_market_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(flow, "build_feature_card", lambda symbol, as_of, *, lookback_days=220, store=None:
                        next((item for item in records if item["kind"] == "market" and item["symbol"] == symbol), None))
    monkeypatch.setattr(flow, "INDICATORS_VERSION", "market-v1")


def _stub_portfolio(monkeypatch):
    policy = PortfolioPolicy(
        constraint_builder=IntentConstraintBuilder(max_position_weight=.9, hold_band=.05, seed_weight=.05, max_holdings=2),
        allocation=AllocationConfig(alpha=1,beta=.1,gamma=.1,max_turnover=1,max_sector_weight=1,sectors={"AAPL":"tech"},liquidity_caps={"AAPL":.9}),
        volatility_target=VolatilityTarget(target_volatility=.2,min_exposure=.1,max_exposure=.9,max_exposure_increase=.9),
        risk_limits=RiskLimits(max_exposure=.9,cash_floor=.1,max_position_weight=.9,max_sector_weight=1,max_turnover=1,max_portfolio_volatility=.5,drawdown_lock=.2,sectors={"AAPL":"tech"}),
        cost_limits=CostLimits(min_order_weight=0,no_trade_band=0,max_turnover=1,max_participation_rate=1,max_cost_rate=1),
        cost_model=CostModel(0,0,0),policy_version="test-policy-v1")
    covariance={"AAPL":{"AAPL":.04}}
    monkeypatch.setattr(flow, "build_policy_and_inputs", lambda *_args: (policy,{"AAPL":.4},{"AAPL":.2},covariance,TradingInputs(100000,50000,{"AAPL":100},{"AAPL":1e6},{"AAPL":1e8}),{"drawdown":0.0,"untradable":[],"stop_loss":[],"risk_locked":False}))


def test_daily_ingests_pool_without_model_or_snapshot(monkeypatch, stores):
    ledger, raw = stores
    _stub_collectors(monkeypatch, [news_record(), market_record()])
    result = run_research_round(FlowOptions(pool=("AAPL",), mode="daily", as_of=AS_OF),
                               model=BoomModel(), ledger=ledger, raw_store=raw)

    assert result.mode == "daily" and result.snapshot is None and result.ingested == 2
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_cards").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_daily_risk_trigger_reuses_card_pool_and_runs_decide(monkeypatch, stores):
    ledger, raw = stores
    _stub_collectors(monkeypatch, [news_record(), market_record()])
    _stub_portfolio(monkeypatch)
    account = dict(ACCOUNT)
    monkeypatch.setattr(flow, "load_risk_state", lambda *_args, **_kwargs: {
        "peak_equity": 50_000.0, "drawdown": 0.0, "stop_loss": ["AAPL"], "risk_locked": False,
    })

    result = run_research_round(FlowOptions(pool=("AAPL",), mode="daily", as_of=AS_OF, account_state=account),
                                model=FlowModel(), ledger=ledger, raw_store=raw)

    assert result.mode == "daily" and result.risk_triggered
    assert result.intent is not None and result.portfolio is not None
    assert account["risk_state"]["stop_loss"] == ["AAPL"]


def test_collect_freezes_this_round_cards_and_needs_no_model(monkeypatch, stores):
    ledger, raw = stores
    _stub_collectors(monkeypatch, [news_record(), market_record()])
    result = run_research_round(FlowOptions(pool=("AAPL",), mode="collect", as_of=AS_OF),
                               model=None, ledger=ledger, raw_store=raw)

    assert result.intent is None and result.snapshot is not None
    with ledger._connect() as conn:  # collect 只点名本轮卡，不做窗口选卡
        pool = {row["object_id"] for row in conn.execute("SELECT object_id FROM evidence_cards")}
    assert set(result.snapshot.evidence_ids) == pool


def test_decide_requires_model_and_real_account(stores):
    ledger, raw = stores
    with pytest.raises(ValueError, match="requires an injected model"):
        run_research_round(FlowOptions(pool=("AAPL",), mode="decide", as_of=AS_OF, account_state=ACCOUNT),
                           ledger=ledger, raw_store=raw)
    with pytest.raises(ValueError, match="missing"):
        run_research_round(FlowOptions(pool=("AAPL",), mode="decide", as_of=AS_OF,
                                       account_state={"as_of": AS_OF.isoformat()}),
                           model=FlowModel(), ledger=ledger, raw_store=raw)


def test_decide_selects_cards_from_pool_and_reaches_intent(stores, monkeypatch):
    ledger, raw = stores
    SnapshotBuilder(ledger, raw).ingest_records(
        trace_id="daily-ingest", as_of=AS_OF,
        records=[news_record(), market_record(),
                 # 窗口外的旧卡：daily 期间入池，但决策日 7 天窗口不该把它选进来
                 news_record(title="Stale story", url="https://example.test/old",
                             published_at=AS_OF - timedelta(days=30), available_at=AS_OF - timedelta(days=30))])

    _stub_portfolio(monkeypatch)
    result = run_research_round(FlowOptions(pool=("AAPL",), mode="decide", as_of=AS_OF, account_state=ACCOUNT),
                                model=FlowModel(), ledger=ledger, raw_store=raw)

    assert len(result.snapshot.evidence_ids) == 2 and result.evidence == 2
    assert [packet.symbol for packet in result.packets] == ["AAPL"]
    assert result.packets[0].critic.verdict == "allow"
    assert result.intent.items[0].action == "long"
    # 意图与 Packet 都已入账：定时任务只需回传 ID 就能事后审计
    assert ledger.get_intent(result.intent.intent_id).snapshot_id == result.snapshot.snapshot_id
    assert ledger.get_packet(result.packets[0].packet_id).snapshot_id == result.snapshot.snapshot_id
    assert result.portfolio is not None and ledger.get_order_plan(result.portfolio.order_plan.order_plan_id)


def test_same_as_of_rerun_reports_a_readable_rejection(monkeypatch, stores):
    ledger, raw = stores
    _stub_collectors(monkeypatch, [news_record()])
    options = FlowOptions(pool=("AAPL",), mode="daily", as_of=AS_OF)
    run_research_round(options, model=None, ledger=ledger, raw_store=raw)

    # 账本用 trace 主键拒绝覆盖历史；调度器需要看得懂的句子，而不是 sqlite 栈
    with pytest.raises(ValueError, match="already holds a trace"):
        run_research_round(options, model=None, ledger=ledger, raw_store=raw)
