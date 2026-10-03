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


class PITValidationError(ValueError):
    """带稳定原因码的 PIT 拒绝 / PIT rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
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
        raise PITValidationError("missing_available_at", "source record has no available_at")
    if not isinstance(available_at, datetime):
        raise PITValidationError("invalid_available_at", "available_at must be a datetime")
    if available_at.tzinfo is None or as_of.tzinfo is None:
        raise PITValidationError("timezone_required", "available_at and as_of must be timezone-aware")
    if available_at > as_of:
        raise PITValidationError("future_data", "source record became available after snapshot as_of")
    return available_at


def normalize_record(record: dict[str, Any], *, kind: Literal["news", "filing", "market"], as_of: datetime) -> dict[str, Any]:
    available_at = validate_available_at(record, as_of)
    symbol = str(record.get("symbol", "")).strip().upper()
    source = str(record.get("source", "")).strip()
    if not symbol or not source:
        raise PITValidationError("missing_identity", "source record requires symbol and source")
    normalized = dict(record)
    normalized.update({"symbol": symbol, "source": source, "available_at": available_at, "kind": kind})
    normalized["content_hash"] = canonical_content_hash(normalized)
    return normalized
