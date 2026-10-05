"""阶段一离线验收测试 / Offline acceptance tests for Stage 1.

这些 fixture 只构造内存中的 Provider 记录和隔离 SQLite 文件，用来验证可回放
Snapshot 的核心不变量：PIT 拒绝、转载去重、原始内容可定位、Gateway 边界和
完成 trace 的不可写性。测试不访问网络，也不依赖 Agent 或 PPO。

These fixtures construct only in-memory provider records and isolated SQLite files.
They verify PIT rejection, syndicated-copy deduplication, raw-content location,
Gateway boundaries, and completed-trace immutability. They use neither network,
agents, nor PPO.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.research import DataGateway, ResearchLedger, SnapshotBuilder
from core.research.providers import PITValidationError
from core.research.raw import RawPayloadStore


AS_OF = datetime(2026, 10, 3, 20, tzinfo=timezone.utc)


def build(tmp_path, records):
    ledger = ResearchLedger(tmp_path / "research_ledger.db")
    builder = SnapshotBuilder(ledger, RawPayloadStore(tmp_path / "raw"))
    cards = builder.ingest_records(trace_id="trace-1-ingest", as_of=AS_OF, records=records)
    snapshot = builder.freeze_snapshot(trace_id="trace-1", as_of=AS_OF, symbols=["aapl"], source_versions={"news": "fixture-v1"}, account_state={"cash": 1000}, evidence_ids=tuple(card.evidence_id for card in cards))
    return ledger, snapshot


def news(**updates):
    record = {"kind": "news", "symbol": "AAPL", "source": "fixture", "title": "Earnings beat", "summary": "Revenue increased", "url": "https://example.test/a", "published_at": AS_OF - timedelta(hours=2), "available_at": AS_OF - timedelta(hours=1)}
    record.update(updates)
    return record


def test_snapshot_is_offline_replayable_and_gateway_is_audited(tmp_path):
    ledger, snapshot = build(tmp_path, [news()])
    restored = ledger.get_snapshot(snapshot.snapshot_id)
    cards = DataGateway(ledger).evidence_for_symbol(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF)
    assert restored.model_dump(mode="json") == snapshot.model_dump(mode="json")
    assert len(cards) == 1
    # 指针形态：news://<sha256>，正文在分库中可完整取回并验哈希
    assert cards[0].raw.relative_path == f"news://{cards[0].raw.sha256}"
    payload = RawPayloadStore(tmp_path / "raw").get(cards[0].raw)
    assert payload["title"] == "Earnings beat"


@pytest.mark.parametrize("record, code", [(news(available_at=AS_OF + timedelta(seconds=1)), "future_data"), (news(available_at=None), "missing_available_at")])
def test_pit_rejects_future_or_undated_records(tmp_path, record, code):
    with pytest.raises(PITValidationError, match=code):
        build(tmp_path, [record])


def test_duplicate_reprint_is_deduplicated_by_content_hash(tmp_path):
    ledger, snapshot = build(tmp_path, [news(), news(url="https://mirror.example.test/a")])
    assert len(snapshot.evidence_ids) == 1
    assert len(DataGateway(ledger).evidence_for_symbol(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF)) == 1


def test_gateway_rejects_wrong_symbol_or_as_of(tmp_path):
    ledger, snapshot = build(tmp_path, [news()])
    gateway = DataGateway(ledger)
    with pytest.raises(ValueError, match="symbol"):
        gateway.evidence_for_symbol(snapshot_id=snapshot.snapshot_id, symbol="MSFT", as_of=AS_OF)
    with pytest.raises(ValueError, match="as_of"):
        gateway.evidence_for_symbol(snapshot_id=snapshot.snapshot_id, symbol="AAPL", as_of=AS_OF - timedelta(seconds=1))


def test_completed_trace_is_append_only(tmp_path):
    ledger, snapshot = build(tmp_path, [news()])
    with pytest.raises(ValueError, match="not writable"):
        ledger.append_snapshot(snapshot)


def test_freeze_selects_cards_from_pool_by_window(tmp_path):
    """建卡与冻结分离：每日 ingest 积累的卡片池，决策日按可得窗口选卡冻结。"""
    ledger = ResearchLedger(tmp_path / "research_ledger.db")
    builder = SnapshotBuilder(ledger, RawPayloadStore(tmp_path / "raw"))
    old = news(title="stale story", url="https://example.test/old", published_at=AS_OF - timedelta(days=30), available_at=AS_OF - timedelta(days=30))
    builder.ingest_records(trace_id="ingest-1", as_of=AS_OF - timedelta(days=3), records=[old])
    fresh = news(title="fresh story", url="https://example.test/new", published_at=AS_OF - timedelta(hours=2), available_at=AS_OF - timedelta(hours=1))
    builder.ingest_records(trace_id="ingest-2", as_of=AS_OF, records=[fresh])

    snapshot = builder.freeze_snapshot(trace_id="decide-1", as_of=AS_OF, symbols=["AAPL"], source_versions={"news": "fixture-v1"}, account_state={}, available_from=AS_OF - timedelta(days=7))
    assert len(snapshot.evidence_ids) == 1  # 窗口外的旧卡不入选
    assert ledger.get_evidence(snapshot.evidence_ids[0]).canonical_url == "https://example.test/new"
    # 同一窗口重放选出同一清单（确定性排序）
    again = builder.freeze_snapshot(trace_id="decide-2", as_of=AS_OF, symbols=["AAPL"], source_versions={"news": "fixture-v1"}, account_state={}, available_from=AS_OF - timedelta(days=7))
    assert again.evidence_ids == snapshot.evidence_ids


def test_freeze_without_selection_source_is_rejected(tmp_path):
    ledger = ResearchLedger(tmp_path / "research_ledger.db")
    builder = SnapshotBuilder(ledger, RawPayloadStore(tmp_path / "raw"))
    with pytest.raises(ValueError, match="evidence_ids or available_from"):
        builder.freeze_snapshot(trace_id="bad-freeze", as_of=AS_OF, symbols=["AAPL"], source_versions={}, account_state={})


def test_list_evidence_batches_by_symbol_and_flags_missing_cards(tmp_path):
    """读取侧一次查询完成「限定本 Snapshot 卡集 + 按标的过滤」，引用缺卡必须抛错。

    旧实现逐 ID 往返整池两遍再丢弃非本标的的卡，Gateway 每个读工具都走这条路；
    而缺卡意味着账本损坏，静默少返证据比报错更危险。
    """
    ledger, snapshot = build(tmp_path, [news(), news(title="Peer story", symbol="MSFT", url="https://example.test/b")])
    assert [card.symbol for card in ledger.list_evidence(snapshot, "AAPL")] == ["AAPL"]

    builder = SnapshotBuilder(ledger, RawPayloadStore(tmp_path / "raw"))
    ghost = builder.freeze_snapshot(trace_id="ghost-freeze", as_of=AS_OF, symbols=["AAPL"],
                                    source_versions={"news": "fixture-v1"}, account_state={},
                                    evidence_ids=("ev_missing_card_id",))
    with pytest.raises(KeyError, match="missing evidence"):
        ledger.list_evidence(ghost, "AAPL")


def test_same_story_about_several_symbols_gets_one_card_per_symbol(tmp_path):
    """轮内去重与跨轮查找必须同键（正文哈希 + 标的）。

    只按正文哈希去重时，同一轮里被多个标的引用的同稿只会给第一个标的建卡，
    其余静默丢弃；下一轮却会按 (哈希, 标的) 给它们补建——同一事实的建卡数量
    取决于它恰好和谁排在同一轮，而建卡数量直接决定 Snapshot 内容。
    """
    ledger = ResearchLedger(tmp_path / "research_ledger.db")
    builder = SnapshotBuilder(ledger, RawPayloadStore(tmp_path / "raw"))
    copies = [news(), news(symbol="MSFT", url="https://mirror.example.test/a")]
    cards = builder.ingest_records(trace_id="wire-both", as_of=AS_OF, records=copies)

    assert [card.symbol for card in cards] == ["AAPL", "MSFT"]
    # URL 不同的转载仍是同一事实：两张卡片共用一个正文哈希，只是归属不同标的
    assert cards[0].content_hash == cards[1].content_hash
    assert cards[0].evidence_id != cards[1].evidence_id

    # 同内容再入一轮：去重复用旧卡，不新增行
    again = builder.ingest_records(trace_id="wire-repeat", as_of=AS_OF, records=copies)
    assert [card.evidence_id for card in again] == [card.evidence_id for card in cards]
