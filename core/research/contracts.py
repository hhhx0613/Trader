"""阶段一可审计研究链路的数据契约 / Versioned contracts for audited research.

这里定义写入账本或跨节点传递的不可变 Pydantic 对象。每个对象携带 schema
版本、trace 与创建时间，确保离线回放可辨识其数据格式和来源。它们描述事实
及其引用，不承载模型判断、权重或订单。

This module defines immutable Pydantic objects persisted in the ledger or passed
between nodes. Schema version, trace, and creation time make offline replay
format-aware and attributable. Contracts represent facts and references, never
model judgments, portfolio weights, or orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    """阶段一持久化对象的基础字段 / Common fields for persisted Stage-1 objects."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    trace_id: str
    created_at: datetime = Field(default_factory=utc_now)


class RawReference(BaseModel):
    """原始内容指针 / Pointer to content-addressed raw input.

    SQLite 只保存路径、哈希和来源，避免把大文本复制进账本；读取方可据此验证
    文件未被篡改。

    SQLite stores only path, hash, and source rather than duplicating large text;
    readers use the reference to verify that the file has not been altered.
    """

    model_config = ConfigDict(frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    source: str
    received_at: datetime


class EvidenceCard(Contract):
    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex}")
    kind: Literal["news", "filing", "market"]
    symbol: str
    published_at: datetime | None = None
    available_at: datetime
    source: str
    raw: RawReference
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_url: str | None = None
    duplicate_of: str | None = None
    rejection_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol must not be empty")
        return value


class ResearchSnapshot(Contract):
    snapshot_id: str = Field(default_factory=lambda: f"snap_{uuid4().hex}")
    as_of: datetime
    symbols: tuple[str, ...]
    source_versions: dict[str, str]
    evidence_ids: tuple[str, ...]
    account_state: dict[str, Any]

    @field_validator("symbols")
    @classmethod
    def unique_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(symbol.strip().upper() for symbol in value)
        if not normalized or any(not symbol for symbol in normalized):
            raise ValueError("snapshot requires at least one non-empty symbol")
        if len(set(normalized)) != len(normalized):
            raise ValueError("snapshot symbols must be unique")
        return normalized
