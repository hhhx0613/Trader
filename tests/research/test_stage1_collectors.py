"""阶段一采集/存储主线离线回归 / Offline regression for the mainline collectors and stores.

覆盖讨论定稿的五条主线不变量，全部不联网、不调模型：
  1. coverage 拉取台账：记录"问过哪个窗口"，含空窗口，防止重复烧配额
  2. 正文窗口重放：payloads_in_range 按 symbol+窗口取回并逐条验哈希
  3. 全局卡复用（B 语义）：同一内容同一标的只建一张 EvidenceCard
  4. 行情特征卡：从批次序列算指标；available_at 晚于 as_of 的批次不得进入
  5. 新闻段粒度：历史整段网格省配额；未走完的段拆单日、不得提前封口
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.data.market import _bars_batch_record, build_feature_card, load_price_frame
from core.data.news import _segments_for
from core.research.raw import RawPayloadStore
from core.research.snapshot import SnapshotBuilder
from core.research.store import ResearchLedger

AS_OF = datetime(2026, 10, 3, 20, tzinfo=timezone.utc)


def news_record(**updates):
    record = {
        "kind": "news", "symbol": "AAPL", "source": "fixture", "title": "Earnings beat",
        "summary": "Revenue increased", "url": "https://example.test/a",
        "published_at": AS_OF - timedelta(hours=2), "available_at": AS_OF - timedelta(hours=1),
    }
    record.update(updates)
    return record


def synthetic_bars(symbol, first_date, count, *, base=100.0):
    """构造 count 根等差日线，日期从 first_date 起连续（离线，无随机保证可复算）。"""
    bars = []
    for i in range(count):
        date = (first_date + timedelta(days=i)).strftime("%Y-%m-%d")
        price = base + i
        bars.append({"date": date, "open": price, "high": price + 1, "low": price - 1,
                     "close": price + 0.5, "volume": 1000000 + i})
    return bars


# ---------- 1 & 2: coverage 台账 + 窗口重放 ----------

def test_coverage_records_asked_window_and_replay_returns_payloads(tmp_path):
    store = RawPayloadStore(tmp_path)
    record = news_record()
    store.put(record, source="fixture", received_at=record["available_at"])
    window = (record["published_at"].date().isoformat(), record["published_at"].date().isoformat())
    store.mark_coverage("news", symbol="AAPL", window_start=window[0], window_end=window[1],
                        source="alpha_vantage", fetched_at=record["available_at"], row_count=1)

    assert window in store.covered("news", "aapl")  # symbol 大小写不敏感
    replayed = store.payloads_in_range("news", "AAPL", window[0], window[1])
    assert len(replayed) == 1
    assert replayed[0]["title"] == "Earnings beat"


def test_empty_window_still_counts_as_asked(tmp_path):
    """拉过但 0 条也必须登记：否则每次 capture 都把空段当没拉过、永远重烧配额。"""
    store = RawPayloadStore(tmp_path)
    store.mark_coverage("news", symbol="NVDA", window_start="2026-09-01", window_end="2026-09-28",
                        source="alpha_vantage", fetched_at=AS_OF, row_count=0)
    assert ("2026-09-01", "2026-09-28") in store.covered("news", "NVDA")
    assert store.payloads_in_range("news", "NVDA", "2026-09-01", "2026-09-28") == []


# ---------- 3: 全局卡复用（B 语义） ----------

def test_same_content_reuses_one_card_across_rounds(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.db")
    builder = SnapshotBuilder(ledger, RawPayloadStore(tmp_path / "raw"))

    first = builder.ingest_records(trace_id="round-1", as_of=AS_OF, records=[news_record()])
    later = AS_OF + timedelta(days=7)
    second = builder.ingest_records(trace_id="round-2", as_of=later,
                                    records=[news_record(published_at=later - timedelta(days=6), available_at=later - timedelta(hours=1))])

    # 内容（title/summary/body）一致 -> 同 content_hash -> 复用同一张卡，不重复建行
    assert [card.evidence_id for card in first] == [card.evidence_id for card in second]
    assert ledger.get_evidence(first[0].evidence_id).content_hash == \
        ledger.get_evidence(second[0].evidence_id).content_hash


# ---------- 4: 行情特征卡 + PIT ----------

def test_feature_card_built_from_batch_series(tmp_path):
    store = RawPayloadStore(tmp_path)
    first_date = (AS_OF - timedelta(days=90)).date()
    bars = synthetic_bars("AAPL", first_date, 70)
    batch = _bars_batch_record("AAPL", "yfinance", bars, AS_OF - timedelta(hours=1))
    store.put(batch, source="yfinance", received_at=batch["available_at"])

    frame = load_price_frame("AAPL", first_date.isoformat(), AS_OF.strftime("%Y-%m-%d"), store=store)
    assert len(frame) == 70

    feature = build_feature_card("AAPL", AS_OF, lookback_days=220, store=store)
    assert feature is not None
    assert feature["type"] == "feature"
    assert feature["available_at"] <= AS_OF
    body = feature["body"]
    assert "ema_short" in body and "rsi" in body


def test_feature_card_respects_point_in_time(tmp_path):
    """批次可得时间晚于 as_of -> 那些 bar 当时还不可得 -> 不得出现在特征里。"""
    store = RawPayloadStore(tmp_path)
    bars = synthetic_bars("AAPL", (AS_OF - timedelta(days=90)).date(), 70)
    batch = _bars_batch_record("AAPL", "yfinance", bars, AS_OF + timedelta(days=1))
    store.put(batch, source="yfinance", received_at=batch["available_at"])

    assert build_feature_card("AAPL", AS_OF, lookback_days=220, store=store) is None


@pytest.mark.parametrize("warmup", [10, 40])
def test_feature_card_needs_minimum_warmup(tmp_path, warmup):
    """指标有热身窗口，不足 _MIN_WARMUP_BARS 时不出卡（拒绝用半截序列凑数）。"""
    store = RawPayloadStore(tmp_path)
    bars = synthetic_bars("AAPL", (AS_OF - timedelta(days=warmup + 5)).date(), warmup)
    batch = _bars_batch_record("AAPL", "yfinance", bars, AS_OF - timedelta(hours=1))
    store.put(batch, source="yfinance", received_at=batch["available_at"])
    assert build_feature_card("AAPL", AS_OF, lookback_days=220, store=store) is None


# ---------- 5: 新闻段粒度（实盘单日段 vs 历史网格段） ----------

def _covered_days(windows):
    days = set()
    for ws, we in windows:
        d = pd.Timestamp(ws)
        while d <= pd.Timestamp(we):
            days.add(d.strftime("%Y-%m-%d"))
            d += pd.Timedelta(days=1)
    return days


def test_finished_history_still_uses_whole_grid_segments():
    """全历史区间：仍按整网格段询问，一次请求罩住一段，省配额不变。"""
    windows = _segments_for("2025-08-11", "2025-09-30", today="2026-10-03")
    assert windows
    assert all(pd.Timestamp(we) - pd.Timestamp(ws) == pd.Timedelta(days=27) for ws, we in windows)
    # 段间无缝无叠：下一段起点恰为上一段终点+1
    for prev, nxt in zip(windows, windows[1:]):
        assert pd.Timestamp(nxt[0]) == pd.Timestamp(prev[1]) + pd.Timedelta(days=1)


def test_live_window_splits_unfinished_segment_into_closed_days():
    """未走完的段绝不整段封口：只出现已收口的单日段，今天/未来不得入窗。"""
    windows = _segments_for("2026-09-26", "2026-10-03", today="2026-10-03")
    assert windows
    assert all(ws == we for ws, we in windows)  # 该区间落在未走完段内 -> 全是单日段
    assert {"2026-09-26", "2026-09-30", "2026-10-02"} <= _covered_days(windows)
    assert "2026-10-03" not in _covered_days(windows)  # 未收口的日子不问
    # 单日段边界与请求区间无关：另一区间问同一天，段名相同，coverage 可命中
    other = _segments_for("2026-10-01", "2026-10-02", today="2026-10-03")
    assert ("2026-10-02", "2026-10-02") in other


def test_mixed_window_keeps_whole_finished_head_and_daily_tail():
    """跨走完/未走完段：头部整段网格、尾部单日，两种粒度混存且绝不越今天。"""
    windows = _segments_for("2026-08-25", "2026-10-03", today="2026-10-03")
    whole = [(ws, we) for ws, we in windows if ws != we]
    daily = [(ws, we) for ws, we in windows if ws == we]
    assert whole and daily
    assert all(pd.Timestamp(we) < pd.Timestamp("2026-10-03") for _, we in whole)
    assert "2026-08-25" in _covered_days(windows)  # 尾部日子也罩住
