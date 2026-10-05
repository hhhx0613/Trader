"""研究正文的内容寻址存储 / Content-addressed raw-payload storage.

正文按数据源分库（root 目录下三个 SQLite 文件），表内以 SHA-256 为主键，
相同内容只存一份；账本里的 EvidenceCard 只持有指针，不嵌正文——同一事实
被多个 trace 引用时（周度轮/临时复核/回放实验），卡片各一行、正文仅一份。

三种指针形态：
  news://<sha256> / market://<sha256> / filing://<sha256>
历史文件路径（raw/<hash>/payload.json）在 get() 中仍兼容解析，供旧账本行回放。

append-only 由数据库触发器强制：payloads 表拒绝 UPDATE/DELETE。
读取时重算 SHA-256 与指针比对，静默篡改/漂移当场报错。
本模块不负责联网下载、字段规范化或 PIT 判断。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import RawReference

# EvidenceCard.kind -> 库文件名。filing 类正文放 filings.db；指针 scheme 直接用 kind。
_SCOPES = {"news": "news.db", "market": "market.db", "filing": "filings.db"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payloads (
    sha256       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    symbol       TEXT,
    window_start TEXT,
    window_end   TEXT,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payloads_symbol_window ON payloads(symbol, window_start, window_end);
CREATE TRIGGER IF NOT EXISTS payloads_no_update BEFORE UPDATE ON payloads
BEGIN SELECT RAISE(ABORT, 'payloads table is append-only'); END;
CREATE TRIGGER IF NOT EXISTS payloads_no_delete BEFORE DELETE ON payloads
BEGIN SELECT RAISE(ABORT, 'payloads table is append-only'); END;
CREATE TABLE IF NOT EXISTS coverage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    row_count    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverage_symbol ON coverage(symbol, window_start, window_end);
"""
# coverage 只增：拉取台账不存在"改写历史"，重复询问会多拉一次配额而非谎报已拉。
_COVERAGE_GUARD = """
CREATE TRIGGER IF NOT EXISTS coverage_no_update BEFORE UPDATE ON coverage
BEGIN SELECT RAISE(ABORT, 'coverage table is append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_no_delete BEFORE DELETE ON coverage
BEGIN SELECT RAISE(ABORT, 'coverage table is append-only'); END;
"""


class RawPayloadStore:
    """按 kind 分库、以 SHA-256 去重持久化规范 JSON。root 为三个 db 文件所在目录。

    连接按库名缓存复用：建库 DDL 只在首次打开时执行一次，避免每次读写都
    executescript（存量导入几十万条时这是数量级的差距）。调用方用完调 close()。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._conns: dict[str, sqlite3.Connection] = {}
        # LangGraph 的并行节点在不同 worker 线程访问同一 store：单条 sqlite3.Connection
        # 不能并发执行语句，故每个分库配一把可重入锁串行化读写；_registry 只守护连接/锁的懒创建。
        self._locks: dict[str, threading.RLock] = {}
        self._registry = threading.Lock()

    def _conn(self, db_name: str) -> sqlite3.Connection:
        conn = self._conns.get(db_name)
        if conn is not None:
            return conn
        with self._registry:
            conn = self._conns.get(db_name)
            if conn is None:
                self.root.mkdir(parents=True, exist_ok=True)
                # 正文由本对象跨线程持有，故允许连接跨线程读取，而不是把数据库句柄暴露给 Agent；
                # 真正的并发安全由 _access 的每库锁保证，check_same_thread=False 只是前提。
                conn = sqlite3.connect(self.root / db_name, check_same_thread=False)
                conn.executescript(_SCHEMA + _COVERAGE_GUARD)
                self._conns[db_name] = conn
                self._locks[db_name] = threading.RLock()
        return conn

    @contextmanager
    def _access(self, db_name: str):
        """在同一分库锁内开启事务：串行化跨线程语句，保持 append-only 与哈希校验语义。"""
        conn = self._conn(db_name)
        with self._locks[db_name], conn:
            yield conn

    def close(self) -> None:
        with self._registry:
            for db_name, conn in self._conns.items():
                with self._locks[db_name]:
                    conn.close()
            self._conns.clear()
            self._locks.clear()

    def __enter__(self) -> "RawPayloadStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def put(self, payload: dict[str, Any], *, source: str, received_at: datetime | None = None) -> RawReference:
        kind = str(payload.get("kind", ""))
        db_name = _SCOPES.get(kind)
        if db_name is None:
            raise ValueError(f"payload kind must be one of {sorted(_SCOPES)}, got {kind!r}")
        # ISO-8601 makes timestamps replayable and keeps the content hash stable.
        # ISO-8601 保留可回放时间语义，同时保证内容哈希稳定。
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        received_at = received_at or datetime.now(timezone.utc)
        symbol, window_start, window_end = _window_of(payload)
        with self._access(db_name) as conn:
            # 内容寻址：同哈希即同一事实，重复写入静默跳过（append-only 允许）。
            conn.execute(
                "INSERT OR IGNORE INTO payloads (sha256, source, received_at, symbol, window_start, window_end, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest, source, received_at.isoformat(), symbol, window_start, window_end, canonical),
            )
        return RawReference(
            sha256=digest,
            relative_path=f"{kind}://{digest}",
            source=source,
            received_at=received_at,
        )

    def get(self, reference: RawReference) -> dict[str, Any]:
        content = self._read_pointer(reference)
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError(f"raw payload hash mismatch: {reference.relative_path}")
        return json.loads(content)

    def _read_pointer(self, reference: RawReference) -> bytes:
        pointer = reference.relative_path
        if "://" in pointer:
            kind, digest = pointer.split("://", 1)
            db_name = _SCOPES.get(kind)
            if db_name is None:
                raise ValueError(f"unknown raw pointer scope: {pointer}")
            with self._access(db_name) as conn:
                row = conn.execute("SELECT payload FROM payloads WHERE sha256 = ?", (digest,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"raw payload not found: {pointer}")
            return row[0].encode("utf-8")
        # 旧版文件指针（raw/<hash>/payload.json）：文件还在就直接读；
        # 已被导入脚本搬走则按哈希回落到对应正文库。
        legacy = self.root.parent / pointer
        if legacy.is_file():
            return legacy.read_bytes()
        for kind, db_name in _SCOPES.items():
            if not (self.root / db_name).exists():
                continue
            with self._access(db_name) as conn:
                row = conn.execute("SELECT payload FROM payloads WHERE sha256 = ?", (reference.sha256,)).fetchone()
            if row is not None:
                return row[0].encode("utf-8")
        raise FileNotFoundError(f"raw payload not found: {pointer}")

    # ==================== 窗口查询与拉取台账 ====================

    def mark_coverage(self, kind: str, *, symbol: str, window_start: str, window_end: str, source: str, fetched_at: datetime, row_count: int) -> None:
        """记录一次向数据源的询问（含空窗口），防止重复烧配额。"""
        db_name = _require_scope(kind)
        with self._access(db_name) as conn:
            conn.execute(
                "INSERT INTO coverage (symbol, window_start, window_end, source, fetched_at, row_count) VALUES (?, ?, ?, ?, ?, ?)",
                (symbol.upper(), window_start, window_end, source, fetched_at.isoformat(), row_count),
            )

    def covered(self, kind: str, symbol: str) -> list[tuple[str, str]]:
        """返回该标的已询问过的窗口列表（供采集器算差集）。"""
        db_name = _require_scope(kind)
        with self._access(db_name) as conn:
            rows = conn.execute(
                "SELECT window_start, window_end FROM coverage WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def payloads_in_range(self, kind: str, symbol: str, window_start: str, window_end: str) -> list[dict[str, Any]]:
        """重放：取回窗口重叠过的正文（逐条验哈希，静默漂移当场报错）。"""
        db_name = _require_scope(kind)
        with self._access(db_name) as conn:
            rows = conn.execute(
                "SELECT sha256, payload FROM payloads WHERE symbol = ? AND window_start <= ? AND window_end >= ?",
                (symbol.upper(), window_end, window_start),
            ).fetchall()
        out = []
        for digest, text in rows:
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                raise ValueError(f"raw payload hash mismatch during replay: {kind}://{digest}")
            out.append(json.loads(text))
        return out


def _require_scope(kind: str) -> str:
    db_name = _SCOPES.get(kind)
    if db_name is None:
        raise ValueError(f"payload kind must be one of {sorted(_SCOPES)}, got {kind!r}")
    return db_name


def _window_of(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """提取扁平查询列：优先用显式 window 字段（行情批次），否则退到 published_at 当日。"""
    symbol = str(payload.get("symbol") or "").upper() or None
    start = payload.get("window_start") or _date_str(payload.get("published_at"))
    end = payload.get("window_end") or _date_str(payload.get("published_at"))
    return symbol, start, end


def _date_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]
