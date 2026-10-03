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
    snapshot = builder.build(trace_id="trace-1", as_of=AS_OF, symbols=["aapl"], source_versions={"news": "fixture-v1"}, account_state={"cash": 1000}, records=records)
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
    assert (tmp_path / cards[0].raw.relative_path).is_file()


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
