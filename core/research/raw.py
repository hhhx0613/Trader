"""研究账本的原始载荷存储 / Content-addressed raw-payload storage for the ledger.

每份规范化载荷以 SHA-256 放在 ``raw/<hash>/payload.json``，相同内容只写入
一次。账本仅保留 RawReference，因此 Snapshot 可离线定位、读取并复核原始
事实。这里不负责联网下载、字段规范化或 PIT 判断。

Each normalized payload is stored once at ``raw/<hash>/payload.json``. The ledger
keeps only a RawReference, allowing snapshots to locate, read, and verify facts
offline. This module does not download data, normalize provider fields, or make
PIT decisions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import RawReference


class RawPayloadStore:
    """按 SHA-256 持久化规范 JSON / Persist canonical JSON under its SHA-256 digest."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, payload: dict[str, Any], *, source: str, received_at: datetime | None = None) -> RawReference:
        # ISO-8601 makes timestamps replayable and keeps the content hash stable.
        # ISO-8601 保留可回放时间语义，同时保证内容哈希稳定。
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        path = self.root / digest / "payload.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(canonical)
        received_at = received_at or datetime.now(timezone.utc)
        return RawReference(
            sha256=digest,
            relative_path=path.relative_to(self.root.parent).as_posix(),
            source=source,
            received_at=received_at,
        )

    def get(self, reference: RawReference) -> dict[str, Any]:
        path = self.root.parent / reference.relative_path
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError(f"raw payload hash mismatch: {reference.relative_path}")
        return json.loads(content)
