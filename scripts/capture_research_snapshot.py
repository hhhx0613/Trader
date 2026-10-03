#!/usr/bin/env python3
"""
每日采集 -> 正文落库 -> 建卡（ingest）-> 按需冻结 Snapshot（主线唯一入口）

用法（conda trader 环境）：
  python scripts/capture_research_snapshot.py --pool NVDA AAPL
  python scripts/capture_research_snapshot.py                       # 默认 10 只票
  python scripts/capture_research_snapshot.py --as-of 2026-10-03T18:00:00+00:00
  python scripts/capture_research_snapshot.py --no-snapshot         # 每日节奏：只建卡不冻结

职责边界：
  - 本脚本只做「编排」：调采集器拿记录 -> ingest_records 建卡 ->
    （默认再）freeze_snapshot 冻结；建卡与冻结分属两个 trace，周度决策日
    也可单独走池选卡冻结（SnapshotBuilder.freeze_snapshot 传 available_from）
  - 网络、落库、PIT 校验、去重全部委派给 core/data/*（采集）与 core/research（建卡）
  - 行情按「素材批次 + 每周特征卡」口径：K 线批次不进 Snapshot，
    只有 build_feature_card 产出的特征记录才是 Market Agent 可引用的事实
  - 不触模型、不产意图：Committee 属于后续阶段
"""

import sys
from pathlib import Path

# 让直接运行（python scripts/xxx.py）也能找到 core 模块
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
from datetime import datetime, timedelta, timezone

from core import config
from core.data import filings as filings_collector
from core.data import news as news_collector
from core.data.market import INDICATORS_VERSION, build_feature_card, ensure_market_coverage
from core.research.raw import RawPayloadStore
from core.research.snapshot import SnapshotBuilder
from core.research.store import ResearchLedger

# 与 data_fetch_tracker 一致的默认观察池（保持脚本间可对照）
DEFAULT_POOL = ["AAPL", "AMZN", "GOOGL", "JNJ", "JPM", "MSFT", "NVDA", "UNH", "V", "WMT"]

# 采集窗口（日历天数）：集中一处，便于实验复现与口径追踪
NEWS_LOOKBACK_DAYS = 7      # 每日增量：只补最近一周的段（coverage 命中不重烧配额）
MARKET_LOOKBACK_DAYS = 220  # 特征卡需要足够长序列算指标（与 build_feature_card 默认一致）


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日采集并冻结研究 Snapshot")
    parser.add_argument("--pool", nargs="+", default=DEFAULT_POOL, help="股票池（默认 10 只）")
    parser.add_argument("--as-of", default=None, help="决策时点 ISO-8601（默认当前 UTC）")
    parser.add_argument("--trace-prefix", default="capture", help="trace_id 前缀")
    parser.add_argument("--no-filings", action="store_true", help="跳过 SEC EDGAR 披露采集")
    parser.add_argument("--no-snapshot", action="store_true", help="只建卡（每日节奏），不冻结 Snapshot")
    return parser.parse_args()


def _utc_now_or_parse(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main() -> int:
    args = _parse_args()
    as_of = _utc_now_or_parse(args.as_of)
    pool = [s.upper() for s in args.pool]

    # trace_id = 前缀 + as_of 秒级：建卡与冻结各占一个 trace，同一秒重复运行
    # 会命中已完成 trace 而被账本拒绝（防覆盖历史）
    stamp = as_of.strftime("%Y%m%dT%H%M%SZ")
    ingest_trace = f"{args.trace_prefix}-ingest-{stamp}"
    snapshot_trace = f"{args.trace_prefix}-{stamp}"

    store = RawPayloadStore(config.RESEARCH_RAW_DIR)
    ledger = ResearchLedger(config.RESEARCH_LEDGER_PATH)
    builder = SnapshotBuilder(ledger, store)

    as_of_date = as_of.strftime("%Y-%m-%d")
    news_start = (as_of - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    market_start = (as_of - timedelta(days=MARKET_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    records = []
    collected = {"news": 0, "filing": 0, "market_feature": 0}
    for symbol in pool:
        news_records = news_collector.fetch_news_records(symbol, news_start, as_of_date, store=store)
        records.extend(news_records)
        collected["news"] += len(news_records)

        if not args.no_filings:
            filing_records = filings_collector.fetch_filings_records(symbol, news_start, as_of_date)
            records.extend(filing_records)
            collected["filing"] += len(filing_records)

        # 行情素材：保证窗口内 K 线批次已入库（批次不进 Snapshot）
        ensure_market_coverage(symbol, market_start, as_of_date, store=store)

    # 决策时点必须晚于本轮数据到达时刻：实盘模式（未传 --as-of）采集完才定 as_of，
    # 本轮拉到的 bar 本轮就能出卡（冷启动不再空转）；回放模式（传了 --as-of）时点
    # 钉死不动，本轮新拉数据因 available_at 晚于 as_of 被 PIT 闸门正确拒绝。
    if args.as_of is None:
        as_of = datetime.now(timezone.utc)

    for symbol in pool:
        feature = build_feature_card(symbol, as_of, lookback_days=MARKET_LOOKBACK_DAYS, store=store)
        if feature is not None:
            records.append(feature)
            collected["market_feature"] += 1

    print(f"[Capture] {ingest_trace}: 记录 news={collected['news']} filing={collected['filing']} "
          f"market_feature={collected['market_feature']}，pool={len(pool)}")

    cards = builder.ingest_records(trace_id=ingest_trace, as_of=as_of, records=records)
    if args.no_snapshot:
        print(f"[Capture] 建卡完成：本轮涉及 {len(cards)} 张卡（含复用），未冻结 Snapshot")
        store.close()
        return 0

    source_versions = {
        "news": news_collector.NEWS_VERSION,
        "market": INDICATORS_VERSION,
        "filings": filings_collector.FILINGS_VERSION,
    }
    account_state = {"as_of": as_of.isoformat(), "note": "阶段1无账户状态；占位供 Snapshot 冻结"}
    # 本轮冻结直接引用刚拿到的卡集（含复用旧卡），与拆分前行为一致；
    # 池选卡冻结（传 available_from 走 select_evidence）留给决策日专用入口。
    snapshot = builder.freeze_snapshot(
        trace_id=snapshot_trace,
        as_of=as_of,
        symbols=pool,
        source_versions=source_versions,
        account_state=account_state,
        evidence_ids=tuple(card.evidence_id for card in cards),
    )
    print(f"[Capture] 已冻结 Snapshot {snapshot.snapshot_id}，"
          f"evidence={len(snapshot.evidence_ids)}（去重后）")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
