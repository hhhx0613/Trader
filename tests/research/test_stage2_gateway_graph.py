"""阶段二 Gateway 与 LangGraph 离线验收 / Offline Stage-2 acceptance tests.

fixture 只建立冻结的新闻、披露与行情卡片。FakeModel 取代真实 LLM，验证图的
并行收口、引用边界和 abstain 路由，而不触发网络、真实模型或回测。
"""

from datetime import datetime, timedelta, timezone
import json

from agents.research import PerAssetResearchGraph
from agents.research.event_agent import EventAgent
from core.research import DataGateway, ResearchLedger, SnapshotBuilder
from core.research.raw import RawPayloadStore
from agents.research.tools import ResearchTools


AS_OF = datetime(2026, 10, 4, 20, tzinfo=timezone.utc)


class FakeModel:
    """按节点返回可验证 JSON；真实模型接口保持为 chat_json，测试不依赖密钥。"""

    def chat_json(self, message, **_kwargs):
        if "Classify relevance" in message:
            return {"claims": [{"statement": "Demand event is material", "stance": "bullish", "confidence": 0.7,
                    "supporting_evidence_ids": ["EVENT_ID"]}]}
        if "independent filing evidence" in message:
            return {"claims": [{"statement": "Filing supports revenue growth", "stance": "bullish", "confidence": 0.6,
                    "supporting_evidence_ids": ["FILING_ID"]}]}
        if "priced-in risk" in message:
            return {"claims": [{"statement": "Trend is positive", "stance": "bullish", "confidence": 0.5,
                    "supporting_evidence_ids": ["MARKET_ID"]}]}
        return {"verdict": "allow", "reasons": ["cited claims are consistent"], "card_ids": []}


class FakeToolModel:
    """模拟生产端 function-calling 模型：批次已由注入 hook 放进首轮 prompt，模型只在补查时调 search_evidence。"""

    def chat_json(self, _message, **_kwargs):
        # Risk Critic 已改为纯注入单轮定夺，不再经过 chat_with_tools。
        return {"verdict": "allow", "reasons": ["cited claims are consistent"], "card_ids": []}

    def chat_with_tools(self, message, *, tool_executor, **_kwargs):
        # 断言注入 hook 已生效：模型第一回合就看到 evidence，而不是先调工具才拿到。
        assert "'evidence':" in message
        if message.startswith("{'agent': 'event'"):
            records = json.loads(tool_executor("search_evidence", {"query": "event"}))
            return {"claims": [{"statement": "Demand event is material", "stance": "bullish", "confidence": 0.7,
                    "supporting_evidence_ids": [records[0]["evidence_id"]]}]}
        if message.startswith("{'agent': 'fundamental'"):
            records = json.loads(tool_executor("search_evidence", {"query": "revenue"}))
            return {"claims": [{"statement": "Filing supports revenue growth", "stance": "bullish", "confidence": 0.6,
                    "supporting_evidence_ids": [records[0]["evidence_id"]]}]}
        if message.startswith("{'agent': 'market'"):
            # Market 只读本标的行情卡，不能被事件、同业或 regime 先验锚定。
            assert "'event_theses':" not in message
            assert "'peer_comparison':" not in message
            assert "'regime':" not in message
            records = json.loads(tool_executor("search_evidence", {"query": "trend"}))
            return {"claims": [{"statement": "Trend is positive", "stance": "bullish", "confidence": 0.5,
                    "supporting_evidence_ids": [records[0]["evidence_id"]]}]}
        raise AssertionError(f"unexpected tool-calling node: {message[:40]}")


class DataRequestModel:
    """批次已注入但模型仍判不足以立论：一次性走 data_request，而非无限补查。"""

    def chat_json(self, _message, **_kwargs):
        return {"route": "data_request", "reason_code": "insufficient_evidence",
                "detail": "need a PIT-valid issuer announcement"}


def record(kind, title, body, *, source="fixture"):
    return {"kind": kind, "symbol": "AAPL", "source": source, "title": title, "summary": "summary",
            "body": body, "url": "https://example.test/item", "published_at": AS_OF - timedelta(hours=2),
            "available_at": AS_OF - timedelta(hours=1)}


def frozen(tmp_path, records):
    ledger = ResearchLedger(tmp_path / "ledger.db")
    raw = RawPayloadStore(tmp_path / "raw")
    builder = SnapshotBuilder(ledger, raw)
    cards = builder.ingest_records(trace_id="ingest", as_of=AS_OF, records=records)
    snapshot = builder.freeze_snapshot(trace_id="snapshot", as_of=AS_OF, symbols=["AAPL"],
                                       source_versions={"fixture": "v1"}, account_state={"cash": 1000},
                                       evidence_ids=tuple(card.evidence_id for card in cards))
    return ledger, raw, snapshot, cards


def test_search_evidence_ranks_and_caps_top_k(tmp_path):
    """OR 命中但按相关度排序：命中查询词更多的卡浮到最前。"""
    ledger, raw, snapshot, cards = frozen(tmp_path, [
        record("news", title="Nvidia buyback expansion", body="Nvidia boosts repurchase and buyback"),
        record("news", title="Nvidia fab capacity", body="foundry capacity note"),
        record("news", title="Sector buyback roundup", body="industry repurchase flows"),
    ])
    gateway = DataGateway(ledger, raw)
    common = {"snapshot_id": snapshot.snapshot_id, "symbol": "AAPL", "as_of": AS_OF}

    ranked = gateway.search_evidence(query="Nvidia buyback", **common)
    ids = [item["evidence_id"] for item in ranked]
    assert ids[0] == cards[0].evidence_id  # 同时命中两词，相关度最优先
    assert set(ids) == {card.evidence_id for card in cards}  # 其余仅命中一词，OR 仍保留


def test_search_evidence_caps_frequent_token(tmp_path):
    """单个高频词命中整批时必须截断：默认 top-8，可传 k 收窄，超上限被夹住。"""
    flood = [record("news", title=f"Nvidia note {i}", body=f"NVDA detail {i}") for i in range(25)]
    ledger, raw, snapshot, cards = frozen(tmp_path, flood)
    gateway = DataGateway(ledger, raw)
    common = {"snapshot_id": snapshot.snapshot_id, "symbol": "AAPL", "as_of": AS_OF}

    assert len(gateway.search_evidence(query="nvda", **common)) == 8
    assert len(gateway.search_evidence(query="nvda", k=3, **common)) == 3
    assert len(gateway.search_evidence(query="nvda", k=100, **common)) == 20  # 夹到 _MAX_SEARCH_RESULTS


def test_gateway_returns_cited_excerpts_and_audits_each_query(tmp_path):
    ledger, raw, snapshot, cards = frozen(tmp_path, [record("news", "news", "event"),
                                                       record("filing", "filing", "revenue"),
                                                       record("market", "market", "trend")])
    gateway = DataGateway(ledger, raw)
    news = gateway.get_news_batch(snapshot_id=snapshot.snapshot_id, symbol="aapl", as_of=AS_OF)
    filings = gateway.get_filing_section(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF,
                                         section="revenue")
    market = gateway.get_market_slice(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF)

    assert news[0]["evidence_id"] == cards[0].evidence_id
    assert filings[0]["evidence_id"] == cards[1].evidence_id
    assert market[0]["evidence_id"] == cards[2].evidence_id
    with ledger._connect() as conn:
        kinds = {row["query_kind"] for row in conn.execute("SELECT query_kind FROM gateway_queries")}
    assert {"news_batch", "filing_section", "market_slice"} <= kinds
    assert "relative_path" not in news[0]


def test_graph_builds_packet_after_parallel_claims(tmp_path):
    ledger, raw, snapshot, cards = frozen(tmp_path, [record("news", "news", "event"),
                                                       record("filing", "filing", "revenue"),
                                                       record("market", "market", "trend")])
    model = FakeModel()
    # 让 fake 使用本轮真实 Card ID；生产模型只能引用 Gateway 已提供的 ID。
    ids = {card.kind: card.evidence_id for card in cards}
    original = model.chat_json

    def with_ids(message, **kwargs):
        result = original(message, **kwargs)
        if "claims" in result:
            claim = result["claims"][0]
            if "Classify relevance" in message:
                claim["supporting_evidence_ids"] = [ids["news"]]
            elif "independent filing evidence" in message:
                claim["supporting_evidence_ids"] = [ids["filing"]]
            elif "priced-in risk" in message:
                claim["supporting_evidence_ids"] = [ids["market"]]
        return result

    model.chat_json = with_ids
    packet = PerAssetResearchGraph(ledger=ledger, gateway=DataGateway(ledger, raw), model=model).run(
        snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF, trace_id="research")
    assert len(packet.claim_card_ids) == 3
    assert packet.critic.verdict == "allow"


def test_graph_researches_market_without_news(tmp_path):
    """混合拓扑验收：无新闻时 Event 节点级弃权、Fundamental 无论点可核验而跳过，
    但 Market 独立并行产出行情判断，不被 Event 连坐。"""
    ledger, raw, snapshot, cards = frozen(tmp_path, [record("market", "market", "trend")])
    model = FakeModel()
    ids = {card.kind: card.evidence_id for card in cards}
    original = model.chat_json

    def with_ids(message, **kwargs):
        result = original(message, **kwargs)
        if "claims" in result and "priced-in risk" in message:
            result["claims"][0]["supporting_evidence_ids"] = [ids["market"]]
        return result

    model.chat_json = with_ids
    packet = PerAssetResearchGraph(ledger=ledger, gateway=DataGateway(ledger, raw), model=model).run(
        snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF, trace_id="research")
    # 仅一张 Market ClaimCard：无 event_claims/fundamental_claims。
    assert len(packet.claim_card_ids) == 1
    assert ledger.get_claim(packet.claim_card_ids[0]).agent == "market"
    assert packet.citation_coverage == 1.0


def test_research_tools_binds_snapshot_and_audits_recheck(tmp_path):
    """模型只能给业务参数；伪造 snapshot/symbol 不属于工具 schema，不能越过当前冻结边界。"""
    ledger, raw, snapshot, cards = frozen(tmp_path, [record("news", "news", "event")])
    state = {"trace_id": "research", "snapshot_id": snapshot.snapshot_id, "symbol": "AAPL", "as_of": AS_OF}
    ledger.start_trace("research", AS_OF)
    tools = ResearchTools(gateway=DataGateway(ledger, raw), state=state, node="event",
                          allowed_tools=("search_evidence",), max_calls=2)

    response = json.loads(tools.execute("search_evidence", {"query": "event"}))
    assert response[0]["evidence_id"] == cards[0].evidence_id
    assert tools.evidence_ids == {cards[0].evidence_id}
    try:
        tools.execute("search_evidence", {"query": "event", "snapshot_id": "other"})
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("research tools accepted a model-controlled snapshot boundary")
    # 类别取数不再是可挂载工具：注册它必须报错，防止模型重新枚举式请求。
    try:
        ResearchTools(gateway=DataGateway(ledger, raw), state=state, node="event",
                      allowed_tools=("get_news_batch",), max_calls=2)
    except ValueError:
        pass
    else:
        raise AssertionError("category fetch tool must no longer be model-mountable")
    with ledger._connect() as conn:
        row = conn.execute("SELECT trace_id, node, tool_name, arguments, evidence_ids FROM tool_calls").fetchone()
    assert row["trace_id"] == "research" and row["node"] == "event" and row["tool_name"] == "search_evidence"
    assert json.loads(row["arguments"]) == {"query": "event"}
    assert json.loads(row["evidence_ids"]) == [cards[0].evidence_id]


def test_research_tools_lock_recheck_to_node_kind(tmp_path):
    """补查维度锁：market 节点不传 kinds 只搜行情类；显式跨到新闻类被拒，schema 也收窄。"""
    ledger, raw, snapshot, cards = frozen(tmp_path, [record("news", "news", "event"),
                                                       record("market", "market", "trend")])
    state = {"trace_id": "research_kinds", "snapshot_id": snapshot.snapshot_id, "symbol": "AAPL", "as_of": AS_OF}
    ledger.start_trace("research_kinds", AS_OF)
    tools = ResearchTools(gateway=DataGateway(ledger, raw), state=state, node="market",
                          allowed_tools=("search_evidence",), max_calls=2, allowed_kinds=("market",))
    # schema 里的 enum 已收窄：模型从一开始就看不到越界选项。
    assert tools.tools[0]["function"]["parameters"]["properties"]["kinds"]["items"]["enum"] == ["market"]
    # 不传 kinds 时默认取本节点维度：只可能命中行情卡，碰不到新闻。
    response = json.loads(tools.execute("search_evidence", {"query": "event trend"}))
    assert [item["kind"] for item in response] == ["market"]
    assert response[0]["evidence_id"] == cards[1].evidence_id
    try:
        tools.execute("search_evidence", {"query": "event", "kinds": ["news"]})
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("market node crossed kinds into another evidence category")


def test_graph_injects_batches_and_recheck_is_the_only_model_tool(tmp_path):
    ledger, raw, snapshot, _ = frozen(tmp_path, [record("news", "news", "event"),
                                                   record("filing", "filing", "revenue"),
                                                   record("market", "market", "trend")])
    packet = PerAssetResearchGraph(ledger=ledger, gateway=DataGateway(ledger, raw), model=FakeToolModel()).run(
        snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF, trace_id="research_tools")
    assert len(packet.claim_card_ids) == 3
    with ledger._connect() as conn:
        tools = {row["tool_name"] for row in conn.execute("SELECT tool_name FROM tool_calls")}
        kinds = {row["query_kind"] for row in conn.execute("SELECT query_kind FROM gateway_queries")}
    # 类别取数走注入 hook（gateway_queries 审计），模型侧 function-calling 工具只剩 search_evidence。
    assert tools == {"search_evidence"}
    assert {"news_batch", "filing_section", "market_slice"} <= kinds
    # Critic 的四项上下文全部是注入读取（Plan.md 4.2），每项都得留下审计痕迹；
    # 「不得跨维读取」只能按节点粒度断言，已由 FakeToolModel 里的 prompt 检查把关。
    assert {"current_portfolio", "turnover_cost", "regime", "peer_comparison"} <= kinds


def test_model_can_request_more_data_without_silent_empty_claim(tmp_path):
    """有批次证据但模型仍判不足以立论时，走一次性 data_request，不硬编半成品 claim。"""
    ledger, raw, snapshot, _ = frozen(tmp_path, [record("news", "news", "event")])
    ledger.start_trace("research_request", AS_OF)
    state = {"trace_id": "research_request", "snapshot_id": snapshot.snapshot_id, "symbol": "AAPL", "as_of": AS_OF}
    result = EventAgent(ledger=ledger, gateway=DataGateway(ledger, raw), model=DataRequestModel())(state)
    assert result["event_route"].route == "data_request"
    assert result["event_route"].reason_code == "insufficient_evidence"
    assert "event_claims" not in result


def test_gateway_exposes_no_acquisition_surface(tmp_path):
    """只读边界需要名字层面的约束：Gateway 不再暴露任何联网补齐入口。

    缺证据的唯一出路是 data_request 路由弃权，由下一轮采集脚本补齐后重新冻结。
    如果将来要往 Gateway 上加回 ensure_* 或 run 生命周期，得先有一份说明
    “为什么又需要联网”的 Plan.md 修改，而不是直接删掉本用例。
    """
    gateway = DataGateway(ResearchLedger(tmp_path / "ledger.db"), RawPayloadStore(tmp_path / "raw"))
    public = {name for name in dir(gateway) if not name.startswith("_")}

    assert not [name for name in public if name.startswith("ensure_") or name.endswith("_research_run")]
    assert not hasattr(gateway, "_runs")
    assert {"get_news_batch", "search_evidence", "get_market_slice", "evidence_for_symbol"} <= public


def test_reads_never_touch_the_network_even_for_uncovered_windows(tmp_path, monkeypatch):
    """窗口里没有冻结卡时只能得到空列表，不能默默去拉新数据。"""
    ledger, raw, snapshot, _ = frozen(tmp_path, [])
    gateway = DataGateway(ledger, raw)
    called = []

    def fail_fetch(*args, **kwargs):
        called.append(args)
        raise AssertionError("gateway must not call collectors")

    monkeypatch.setattr("core.data.news.fetch_news_records", fail_fetch)
    assert gateway.get_news_batch(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF) == []
    assert gateway.search_evidence(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF, query="anything") == []
    assert called == []
