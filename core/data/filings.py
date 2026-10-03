"""SEC EDGAR 披露数据采集模块（FilingsCollector）

职责：
  1. 从 SEC EDGAR 公开接口获取 filing 索引（10-K/10-Q/8-K）、XBRL 标准化
     财务事实与披露正文，缓存到 data/cache/filings/
  2. 主线记录链路出口 fetch_filings_records() 输出规范化事实记录，交给
     core/research 的 SnapshotBuilder 落 raw 与账本（raw -> EvidenceCard -> Agent）

与其他采集器的关键区别：
  EDGAR 的 acceptance datetime 是 SEC 官方接收时间，即这条披露"当时就
  可得"的权威证明——published_at = available_at = acceptance，是唯一一个
  能给出真·历史可得时间的数据源（新闻/行情只能用本机接收时刻）。

约束（SEC 公平访问政策）：
  - 每个请求必须带标识性 User-Agent（.env SEC_USER_AGENT，格式
    "AppName/版本 contact@example.com"）
  - 请求速率 <= 10 次/秒（本模块用模块级最小间隔节流）
  - 429/5xx 指数退避重试
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from .. import config

SEC_BASE = "https://data.sec.gov"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
# SEC 公平访问：<=10 req/s。取 0.15s 间隔（约 6.7 req/s）留余量。
_MIN_REQUEST_INTERVAL = 0.15
_last_request_at = [0.0]  # 列表以便跨函数共享

# 视为"披露事件"的表格类 filing（8-K=重大事项，10-K/10-Q=定期报告）
DEFAULT_FORMS = frozenset({"10-K", "10-Q", "8-K"})

# 采集口径版本：写入 Snapshot.source_versions，回放时据此锁定当时的解析规则
FILINGS_VERSION = "filings-collector-v1"


def _headers() -> Dict[str, str]:
    ua = os.getenv("SEC_USER_AGENT", "TraderResearch/0.1 contact@example.com")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def _throttle() -> None:
    """全局节流：任意两次 EDGAR 请求间隔不小于 _MIN_REQUEST_INTERVAL。"""
    wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at[0])
    if wait > 0:
        time.sleep(wait)
    _last_request_at[0] = time.monotonic()


def _get_json(url: str, max_retries: int = 3) -> Optional[dict]:
    """带节流与退避重试的 GET JSON；彻底失败返回 None（不抛异常打断采集）。"""
    for attempt in range(max_retries):
        try:
            _throttle()
            response = requests.get(url, headers=_headers(), timeout=30)
            if response.status_code in (429, 500, 502, 503):
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[FilingsCollector] 请求失败（第 {attempt + 1} 次）：{e}")
            time.sleep(2 ** attempt)
    print(f"[FilingsCollector] 放弃请求：{url}")
    return None


def _cached_json(path: Path, fetcher, max_age_days: float) -> Optional[dict]:
    """通用"缓存优先 + 过期重拉"：文件存在且未过期直接读，否则调 fetcher。"""
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < max_age_days * 86400:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass  # 缓存损坏则重拉
    data = fetcher()
    if data is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    return data


# ==================== CIK 映射 ====================

def get_cik(symbol: str) -> Optional[str]:
    """ticker -> 10 位补零 CIK；映射表本地缓存 30 天（SEC 官方映射，很少变）。"""
    cache = config.FILINGS_CACHE_DIR / "company_tickers.json"
    data = _cached_json(
        cache,
        lambda: _get_json("https://www.sec.gov/files/company_tickers.json"),
        max_age_days=30,
    )
    if not data:
        return None
    target = symbol.strip().upper()
    for entry in data.values():
        if str(entry.get("ticker", "")).upper() == target:
            # 官方映射文件的字段名是 cik_str（非 submissions 接口的 cik），写错会静默变 None
            cik = int(entry["cik_str"])
            return f"{cik:010d}"
    print(f"[FilingsCollector] EDGAR 无此 ticker：{symbol}")
    return None


# ==================== 原始数据获取 ====================

def fetch_submissions(symbol: str) -> Optional[dict]:
    """filing 历史索引（含 acceptance datetime /  accession / 正文文件名），缓存 1 天。"""
    cik = get_cik(symbol)
    if cik is None:
        return None
    cache = config.FILINGS_CACHE_DIR / f"{cik}_submissions.json"
    return _cached_json(
        cache,
        lambda: _get_json(f"{SEC_BASE}/submissions/CIK{cik}.json"),
        max_age_days=1,
    )


def fetch_company_facts(symbol: str) -> Optional[dict]:
    """XBRL companyfacts（标准化财务事实，每个观测自带 filed/form/accn），缓存 1 天。"""
    cik = get_cik(symbol)
    if cik is None:
        return None
    cache = config.FILINGS_CACHE_DIR / f"{cik}_companyfacts.json"
    return _cached_json(
        cache,
        lambda: _get_json(f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"),
        max_age_days=1,
    )


def fetch_filing_document(url: str) -> Optional[str]:
    """filing 正文（HTML 原文），按 URL 哈希缓存；正文解读留给后续阶段的 Agent。"""
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20] + ".htm"
    path = config.FILINGS_CACHE_DIR / "docs" / name
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    _throttle()
    try:
        response = requests.get(url, headers=_headers(), timeout=60)
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(response.text, encoding="utf-8")
        return response.text
    except Exception as e:
        print(f"[FilingsCollector] 正文获取失败：{url}（{e}）")
        return None


# ==================== 规范化记录（主线链路） ====================

def _parse_acceptance(value: str) -> Optional[datetime]:
    """acceptanceDateTime 形如 '2026-08-21T20:31:00-04:00'（带官方时区偏移）。"""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _primary_doc_url(cik: str, accession: str, primary_document: str) -> str:
    return f"{ARCHIVES_BASE}/{int(cik)}/{accession.replace('-', '')}/{primary_document}"


def _acceptance_index(submissions: dict, forms) -> List[Tuple[dict, datetime]]:
    """展开发行索引 -> [(条目, acceptance 时间)]，按 forms 过滤、按时间升序。"""
    recent = submissions.get("filings", {}).get("recent", {})
    fields = ["form", "accessionNumber", "acceptanceDateTime", "filingDate", "primaryDocument", "reportDate"]
    rows = list(zip(*(recent.get(f, []) for f in fields))) if recent.get("form") else []
    out = []
    for form, accession, acceptance, filing_date, primary_doc, report_date in rows:
        if forms and form not in forms:
            continue
        accepted = _parse_acceptance(acceptance)
        if accepted is None or not primary_doc:
            continue
        out.append(({"form": form, "accession": accession, "filing_date": filing_date,
                     "primary_document": primary_doc, "report_date": report_date,
                     "acceptance": accepted}, accepted))
    out.sort(key=lambda pair: pair[1])
    return out


def filings_records_from_submissions(
    submissions: dict,
    symbol: str,
    cik: str,
    start_date: str,
    end_date: str,
    forms=DEFAULT_FORMS,
) -> List[Dict]:
    """纯函数：submissions JSON -> 披露索引事实记录（无网络，可离线测试）。

    published_at = available_at = SEC acceptance datetime（官方可得时间）。
    """
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    records = []
    for entry, accepted in _acceptance_index(submissions, forms):
        if not (start <= accepted <= end):
            continue
        url = _primary_doc_url(cik, entry["accession"], entry["primary_document"])
        records.append({
            "kind": "filing",
            "symbol": symbol,
            "source": "SEC EDGAR",
            "title": f"{entry['form']} {entry['report_date'] or entry['filing_date']} ({entry['accession']})",
            "summary": "",
            "body": (f"accession={entry['accession']} form={entry['form']} "
                     f"filed={entry['filing_date']} acceptance={accepted.isoformat()} "
                     f"primaryDocument={entry['primary_document']}"),
            "url": url,
            "published_at": accepted,
            "available_at": accepted,
            "provider_id": entry["accession"],
        })
    return records


def xbrl_records_from_facts(
    facts: dict,
    submissions: dict,
    symbol: str,
    cik: str,
    start_date: str,
    end_date: str,
) -> List[Dict]:
    """纯函数：companyfacts + submissions -> XBRL 观测事实记录。

    只保留 accession 能在 submissions 里对上 acceptance 时间的观测
    （对上 = 能证明当时可得；对不上直接丢弃，不拿 filed 日期凑数）。
    """
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    by_accession = {entry["accession"]: (entry, accepted)
                    for entry, accepted in _acceptance_index(submissions, forms=None)}

    records = []
    for tag, concept in (facts.get("facts", {}).get("us-gaap", {}) or {}).items():
        for unit_values in concept.get("units", {}).values():
            for obs in unit_values:
                accn = obs.get("accn")
                hit = by_accession.get(accn)
                if hit is None:
                    continue
                entry, accepted = hit
                if not (start <= accepted <= end):
                    continue
                records.append({
                    "kind": "filing",
                    "symbol": symbol,
                    "source": "SEC EDGAR XBRL",
                    "title": f"{tag} {obs.get('start', '')}..{obs.get('end')} ({entry['form']})",
                    "summary": "",
                    "body": (f"{symbol} {tag} = {obs.get('val')} "
                             f"period {obs.get('start')}..{obs.get('end')} "
                             f"form={entry['form']} accession={accn}"),
                    "url": _primary_doc_url(cik, accn, entry["primary_document"]),
                    "published_at": accepted,
                    "available_at": accepted,
                    "provider_id": f"{accn}:{tag}:{obs.get('end')}",
                })
    return records


def fetch_filings_records(
    symbol: str,
    start_date: str,
    end_date: str,
    include_xbrl: bool = True,
) -> List[Dict]:
    """主线记录链路入口：窗口内的披露索引记录（+ XBRL 财务事实观测）。

    流水（fetch_* 负责安全拿原料，*_from_* 纯函数负责洗成统一零件）：

        NVDA(股票代码)
           │ get_cik()  查 SEC 内部编号 CIK
           ▼
        CIK0001045810
           │ fetch_submissions() / fetch_company_facts()  联网抓原始 JSON（限速+缓存）
           ▼
        SEC 原始数据（公告清单 + 财务数字）
           │ filings_records_from_submissions() / xbrl_records_from_facts()
           ▼
        事实记录 [{kind, symbol, published_at, available_at, url, ...}]
           → 交给 SnapshotBuilder 建 EvidenceCard

    published_at = available_at = SEC acceptance（官方接收时刻），
    是三类数据源中唯一可信的历史可得时间，PIT 校验以它为准。
    """
    cik = get_cik(symbol)
    submissions = fetch_submissions(symbol)
    if cik is None or submissions is None:
        print(f"[FilingsCollector] {symbol}: EDGAR 索引不可用，主线记录为空")
        return []

    records = filings_records_from_submissions(submissions, symbol, cik, start_date, end_date)
    if include_xbrl:
        facts = fetch_company_facts(symbol)
        if facts is not None:
            records += xbrl_records_from_facts(facts, submissions, symbol, cik, start_date, end_date)

    print(f"[FilingsCollector] {symbol}: 主线记录 {len(records)} 条（{start_date} ~ {end_date}）")
    return records
