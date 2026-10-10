"""主线新闻采集器 / Mainline news collector (fetch -> payload -> record)

数据流（与旧基线 core/legacy/news_data.py 完全分家）：
  1. 按段询问数据源，coverage 表（news.db 内）记录"哪段哪天上问过"：
     已走完的 28 天网格段整段询问（回填省配额）；未走完的段拆成单日段，
     只问已收口的日子——绝不再把提前问到段尾的未走完段整段封口。
     已问过的段永不重烧配额——空段也算"问过"，这正是文件名索引干不了的事
  2. 新段的每条新闻立即落 news.db（内容寻址正文），并返回规范化记录
     （published_at = 发布时间；available_at = 本次网络接收时刻）
  3. 记录交给 SnapshotBuilder 建卡入 ledger.db；崩溃时宁可少记 coverage
     多烧一次配额（重复正文被哈希去重），绝不谎报已拉

约束沿用：AV 免费 25 次/天 + 每分钟约 5 次（段间 sleep 15s），Finnhub 兜底。
旧 CSV 网格缓存（data/cache/news/）不在本模块读取范围内；存量迁移待后续单独安排。
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .. import config
from ..research.providers import canonical_content_hash, record_from_payload
from ..research.raw import RawPayloadStore

# 与旧基线共用网格定义（锚点/段长是数据布局常量，不是逻辑），保证
# 存量导入写出的 coverage 窗口与这里询问的窗口可比对。
_SEGMENT_DAYS = config.NEWS_SEGMENT_DAYS
_ANCHOR = pd.Timestamp(config.NEWS_SEGMENT_ANCHOR)

# 采集口径版本：写入 Snapshot.source_versions，回放时据此锁定当时的解析规则
NEWS_VERSION = "news-collector-v1"


def _default_store() -> RawPayloadStore:
    return RawPayloadStore(config.RESEARCH_RAW_DIR)


def _segment_covered_by_days(seg_start: str, seg_end: str, covered: set) -> bool:
    """整段是否已被逐日覆盖（避免网格收尾时重复烧配额）。"""
    day = pd.Timestamp(seg_start)
    end = pd.Timestamp(seg_end)
    while day <= end:
        d = day.strftime("%Y-%m-%d")
        if (d, d) not in covered:
            return False
        day += pd.Timedelta(days=1)
    return True


def _segments_for(start_date: str, end_date: str, *,
                  today: Optional[str] = None) -> List[Tuple[str, str]]:
    """把请求区间映射到段列表（同一段无论被哪个区间请求都同名）。

    历史回填与实盘逐日共用本函数，按段是否走完自动区分粒度：
      - 网格段已走完（seg_end < today）：整段返回。一次请求罩住一段，
        28 天≈千条上限内，回填省配额；实盘命中同名已问段直接重放。
      - 未走完的段：拆成单日段 (d, d)，且只拆到已收口的日子（d < today）。
        旧规则把包含今天的段整段询问并标记已问，会永久封死段内后续
        天数；逐日单日段让实盘滚动与历史回填自然区分，coverage 混存
        两种段名互不冲突（均为精确匹配的规范边界）。

    today 仅供测试注入；默认取当前 UTC 日期。
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    cutoff = pd.Timestamp(today) if today else pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    k = (start.normalize() - _ANCHOR.normalize()).days // _SEGMENT_DAYS
    seg_start = _ANCHOR.normalize() + pd.Timedelta(days=k * _SEGMENT_DAYS)
    segments = []
    while seg_start <= end:
        seg_end = seg_start + pd.Timedelta(days=_SEGMENT_DAYS - 1)
        if seg_end < cutoff:
            segments.append((seg_start.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        else:
            day = max(start.normalize(), seg_start)
            while day <= end and day < cutoff:
                segments.append((day.strftime("%Y-%m-%d"), day.strftime("%Y-%m-%d")))
                day += pd.Timedelta(days=1)
        seg_start = seg_end + pd.Timedelta(days=1)
    return segments


def _to_utc(value: pd.Timestamp) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")  # AV time_published 与 Finnhub epoch 均为 UTC 语义
    return ts.to_pydatetime()


def _news_record(symbol: str, *, source: str, title: str, summary: str, url: str,
                 published_at: datetime, fetched_at: datetime, extra: Dict[str, Any]) -> Dict[str, Any]:
    """构造与 SnapshotBuilder.normalize_record 逐字节一致的记录。

    采集器写入正文用的就是这个 dict，builder 再 put 同一份规范化记录时
    序列化字节相同 -> 同哈希 -> INSERT OR IGNORE，正文全库只存一份。
    """
    record = {
        "kind": "news",
        "symbol": symbol.upper(),
        "source": source,
        "title": title,
        "summary": summary,
        "url": url,
        "published_at": published_at,
        "available_at": fetched_at,
        "provider_id": url or f"{source}:{published_at.isoformat()}:{title[:40]}",
        **extra,
    }
    record["content_hash"] = canonical_content_hash(record)
    return record


# 采集时过滤低价值新闻：情绪强度不足且无实质话题的条目不建卡，避免拉 1600 条只看 20 条的浪费。
_MIN_SENTIMENT = 0.15
_SUBSTANTIVE_TOPICS = {"earnings", "company", "product", "regulation", "merger",
                        "dividend", "lawsuit", "cpi", "feds", "treasury", "ipo",
                        "supply", "demand", "management", "forecast", "guidance"}


def _fetch_segment_alpha_vantage(symbol: str, seg_start: str, seg_end: str,
                                 fetched_at: datetime) -> Optional[List[Dict[str, Any]]]:
    """AV News & Sentiment；过滤低价值新闻后返回规范化记录，失败返回 None（不标记 coverage）。"""
    api_key = config.ALPHA_VANTAGE_API_KEY
    if not api_key:
        return None
    url = (
        "https://www.alphavantage.co/query"
        "?function=NEWS_SENTIMENT"
        f"&tickers={symbol}"
        f"&time_from={pd.Timestamp(seg_start).strftime('%Y%m%dT0000')}"
        f"&time_to={pd.Timestamp(seg_end).strftime('%Y%m%dT2359')}"
        "&limit=1000"
        f"&apikey={api_key}"
    )
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        feed = response.json().get("feed")
    except Exception as e:
        print(f"[News] Alpha Vantage 段失败 {seg_start}~{seg_end}：{e}")
        return None
    if feed is None:
        # 格式异常/限流响应没有 feed 键：视为失败，下轮重试
        print(f"[News] Alpha Vantage 响应异常 {seg_start}~{seg_end}")
        return None
    records = []
    kept = 0
    for item in feed:
        try:
            published = pd.to_datetime(item["time_published"], format="%Y%m%dT%H%M%S")
        except (KeyError, ValueError):
            continue
        score = float(item.get("overall_sentiment_score", 0.0) or 0.0)
        topics = set(str(t.get("topic", "")).strip().lower() for t in (item.get("topics") or []) if isinstance(t, dict))
        # 过滤门槛：情绪强度不足且无实质话题的不建卡，减少模型无效信息量
        if abs(score) < _MIN_SENTIMENT and not (topics & _SUBSTANTIVE_TOPICS):
            continue
        kept += 1
        records.append(_news_record(
            symbol,
            source=str(item.get("source", "Alpha Vantage")),
            title=item.get("title", ""),
            summary=item.get("summary", ""),
            url=item.get("url", ""),
            published_at=_to_utc(published),
            fetched_at=fetched_at,
            extra={"sentiment_score": score, "topics": list(topics & _SUBSTANTIVE_TOPICS)},
        ))
    dropped = len(feed) - kept
    if dropped:
        print(f"[News] {symbol} {seg_start}~{seg_end}: 保留 {kept} 条，过滤 {dropped} 条低价值")
    return records


def _fetch_segment_finnhub(symbol: str, seg_start: str, seg_end: str,
                           fetched_at: datetime) -> Optional[List[Dict[str, Any]]]:
    """Finnhub company-news 兜底（免费 60 次/天，仅近 30 天）。"""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return None
    try:
        response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": symbol, "from": seg_start, "to": seg_end, "token": api_key},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"[News] Finnhub 段失败 {seg_start}~{seg_end}：{e}")
        return None
    if not isinstance(data, list):
        return None
    records = []
    for item in data:
        if "datetime" not in item:
            continue
        records.append(_news_record(
            symbol,
            source=str(item.get("source", "Finnhub")),
            title=item.get("headline", ""),
            summary=item.get("summary", ""),
            url=item.get("url") or item.get("link") or "",
            published_at=_to_utc(pd.Timestamp(item["datetime"], unit="s")),
            fetched_at=fetched_at,
            extra={"sentiment_score": 0.0},
        ))
    return records


def fetch_news_records(symbol: str, start_date: str, end_date: str, *,
                       store: Optional[RawPayloadStore] = None) -> List[Dict[str, Any]]:
    """主线入口：返回窗口内的新闻记录（拉新段 + 从正文库重放已问段）。"""
    store = store or _default_store()
    covered = set(store.covered("news", symbol))
    records: List[Dict[str, Any]] = []
    api_calls = 0

    for seg_start, seg_end in _segments_for(start_date, end_date):
        if (seg_start, seg_end) in covered or _segment_covered_by_days(seg_start, seg_end, covered):
            # 重放：正文库读回的 payload 回转成记录（哈希稳定，builder 天然去重）
            replayed = [record_from_payload(p)
                        for p in store.payloads_in_range("news", symbol, seg_start, seg_end)]
            records.extend(replayed)
            print(f"[News] {symbol} 段 {seg_start}~{seg_end}：缓存命中 {len(replayed)} 条（库内重放，未烧配额）")
            continue

        if api_calls >= config.NEWS_DAILY_QUOTA:
            print(f"[News] 本次运行配额已用完，{seg_start}~{seg_end} 留待下次")
            continue
        fetched_at = datetime.now(timezone.utc)
        segment_records = _fetch_segment_alpha_vantage(symbol, seg_start, seg_end, fetched_at)
        source = "alpha_vantage"
        if segment_records is None:
            segment_records = _fetch_segment_finnhub(symbol, seg_start, seg_end, fetched_at)
            source = "finnhub"
        api_calls += 1
        if segment_records is None:
            print(f"[News] {symbol} 段 {seg_start}~{seg_end} 两源均失败，不记 coverage（下次重试）")
            time.sleep(15)  # 无论成败都按限流节奏走，失败往往正是限流
            continue

        for record in segment_records:
            store.put(record, source=record["source"], received_at=fetched_at)
        # 先落正文、后记台账：崩溃最多导致重拉（哈希去重兜底），不会谎报已拉
        store.mark_coverage("news", symbol=symbol, window_start=seg_start, window_end=seg_end,
                            source=source, fetched_at=fetched_at, row_count=len(segment_records))
        records.extend(segment_records)
        print(f"[News] {symbol} 段 {seg_start}~{seg_end}：新拉 {len(segment_records)} 条（{source}）")
        if api_calls < config.NEWS_DAILY_QUOTA:
            time.sleep(15)  # AV 免费版约 5 次/分钟

    # 网格段可能拉回超出请求窗口的数据（如 7 天请求落在 28 天网格段内）；
    # 全部存储以保证后续覆盖，但只返回调用方实际请求的时间范围。
    start_ts = pd.Timestamp(start_date).tz_localize("UTC")
    end_ts = pd.Timestamp(end_date).tz_localize("UTC") + pd.Timedelta(days=1)
    records = [r for r in records if start_ts <= r["published_at"] < end_ts]

    return records
