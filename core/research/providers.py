"""Provider 记录规范化与 PIT 校验 / Normalize provider records and validate PIT.

现有新闻、披露和行情 Provider 获取到记录后，应在进入 SnapshotBuilder 前经过
本模块：统一 symbol/source、要求带时区的 available_at，并拒绝晚于 as_of 的
信息。这里是纯函数层，不请求网络、不读写缓存，也不判定交易信号。

Existing news, filing, and market providers pass acquired records through this
module before SnapshotBuilder. It normalizes symbol/source, requires timezone-aware
available_at, and rejects facts newer than as_of. It is pure: no network, cache
I/O, or trading-signal decision occurs here.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Literal

from .reasons import ReasonCode


class PITValidationError(ValueError):
    """带稳定原因码的 PIT 拒绝 / PIT rejection with a stable machine-readable code."""

    def __init__(self, code: ReasonCode | str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_content_hash(record: dict[str, Any]) -> str:
    # Syndicated copies often have different URLs, so normalized content defines identity.
    # 转载通常 URL 不同，因此以规范化正文而非 URL 识别同一事实。
    text = "\n".join(str(record.get(key, "")).strip().lower() for key in ("title", "summary", "body"))
    return hashlib.sha256(re.sub(r"\s+", " ", text).encode("utf-8")).hexdigest()


def validate_available_at(record: dict[str, Any], as_of: datetime) -> datetime:
    available_at = record.get("available_at")
    if available_at is None:
        raise PITValidationError(ReasonCode.MISSING_AVAILABLE_AT, "source record has no available_at")
    if not isinstance(available_at, datetime):
        raise PITValidationError(ReasonCode.INVALID_AVAILABLE_AT, "available_at must be a datetime")
    if available_at.tzinfo is None or as_of.tzinfo is None:
        raise PITValidationError(ReasonCode.TIMEZONE_REQUIRED, "available_at and as_of must be timezone-aware")
    if available_at > as_of:
        raise PITValidationError(ReasonCode.FUTURE_DATA, "source record became available after snapshot as_of")
    return available_at


def   normalize_record(record: dict[str, Any], *, kind: Literal["news", "filing", "market"], as_of: datetime) -> dict[str, Any]:
    available_at = validate_available_at(record, as_of)
    symbol = str(record.get("symbol", "")).strip().upper()
    source = str(record.get("source", "")).strip()
    if not symbol or not source:
        raise PITValidationError(ReasonCode.MISSING_IDENTITY, "source record requires symbol and source")
    normalized = dict(record)
    normalized.update({"symbol": symbol, "source": source, "available_at": available_at, "kind": kind})
    normalized["content_hash"] = canonical_content_hash(normalized)
    return normalized


def record_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """已存正文 -> 可重放记录 / Stored payload back to a replayable record.

    重放路径（coverage 命中后从正文库读回）拿到的是 JSON 字符串时间字段；
    normalize_record 要求 available_at 为 datetime。回转后的记录与原写入记录
    逐字段相等，序列化字节/哈希不变，故后续建卡写入天然去重。
    """
    record = dict(payload)
    for key in ("available_at", "published_at"):
        value = record.get(key)
        if isinstance(value, str):
            record[key] = datetime.fromisoformat(value)
    return record
