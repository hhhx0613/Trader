"""
LLM 缓存数据库管理器（SQLite）

设计说明：
  - 替代原有的 JSON 文件缓存方案
  - 提供 SQL 查询能力，支持复杂筛选和统计
  - 事务保证数据一致性
  - 单文件部署，无需额外数据库服务器

表结构：
  - llm_analysis_cache: 存储 LLM 分析结果
  - 索引：symbol, analysis_date, model, prompt_version（加速查询）
  - UNIQUE 约束包含 prompt_version：prompt/记忆模板一变即触发缓存失效重算
"""

import sqlite3
import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime


class LLMCacheDB:
    """LLM 分析结果缓存数据库"""
    
    def __init__(self, db_path: str = None):
        """
        初始化数据库连接
        
        Args:
            db_path: 数据库文件路径，默认为 core/data/cache/llm_cache.db
        """
        if db_path is None:
            # 默认路径：data/cache/llm/llm_cache.db
            self.db_path = Path(__file__).parent.parent.parent / "data" / "cache" / "llm" / "llm_cache.db"
        else:
            self.db_path = Path(db_path)
        
        # 确保目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 初始化数据库表
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表和索引"""
        with sqlite3.connect(self.db_path) as conn:
            # 创建缓存表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_analysis_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    direction TEXT,
                    confidence REAL,
                    composite_score REAL,
                    reasons TEXT,
                    sources TEXT,
                    prompt_version TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, analysis_date, model, prompt_version)
                )
            """)
            
            # 存量库迁移：旧表无 composite_score 列时补加（Plan.md 假设 7 B 档）
            cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_analysis_cache)")}
            if "composite_score" not in cols:
                conn.execute("ALTER TABLE llm_analysis_cache ADD COLUMN composite_score REAL")

            # 创建索引（加速查询）
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_symbol 
                ON llm_analysis_cache(symbol)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_date 
                ON llm_analysis_cache(analysis_date)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_model 
                ON llm_analysis_cache(model)
            """)
            
            conn.commit()
    
    def get_cache(self, symbol: str, date: str, model: str, prompt_version: str = "") -> Optional[Dict]:
        """
        查询缓存

        按自然日查询，兼容 SQLite 中带午夜时间的 ``analysis_date``。缓存的业务键是
        交易日而非时间字符串，因此调用方传 ``YYYY-MM-DD`` 或带时间的 Timestamp
        都应命中同一条 PIT 信号。

        Args:
            symbol: 股票代码
            date: 分析日期（格式：YYYY-MM-DD）
            model: LLM 模型名称
            prompt_version: prompt 版本哈希（内容级 key 的一部分，prompt 或记忆模板一变即失效）

        Returns:
            缓存的分析结果字典，不存在则返回 None
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT symbol, analysis_date, model, direction, confidence,
                       reasons, sources, prompt_version, created_at, composite_score
                FROM llm_analysis_cache
                WHERE symbol=? AND DATE(analysis_date)=DATE(?) AND model=? AND prompt_version=?
                ORDER BY analysis_date DESC
                LIMIT 1
            """, (symbol, date, model, prompt_version))
            
            row = cursor.fetchone()
            if row:
                return {
                    "symbol": row[0],
                    "analysis_date": row[1],
                    "model": row[2],
                    "direction": row[3],
                    "confidence": row[4],
                    "reasons": json.loads(row[5]) if row[5] else [],
                    "sources": json.loads(row[6]) if row[6] else [],
                    "prompt_version": row[7],
                    "created_at": row[8],
                    "composite_score": row[9],
                }
            return None

    def save_cache(self, data: Dict) -> bool:
        """
        保存缓存（INSERT OR REPLACE）
        
        Args:
            data: 分析结果字典，包含 symbol, analysis_date, model, direction, 
                  confidence, reasons, sources, prompt_version
        
        Returns:
            是否成功保存
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO llm_analysis_cache
                    (symbol, analysis_date, model, direction, confidence, 
                     composite_score, reasons, sources, prompt_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    data["symbol"],
                    data["analysis_date"],
                    data.get("model", "glm-4"),
                    data.get("direction"),
                    data.get("confidence"),
                    data.get("composite_score"),
                    json.dumps(data.get("reasons", []), ensure_ascii=False),
                    json.dumps(data.get("sources", []), ensure_ascii=False),
                    data.get("prompt_version", "v1.0")
                ))
                conn.commit()
            return True
        except Exception as e:
            print(f"[LLM Cache DB] 保存失败：{e}")
            return False
    
    def get_previous_analysis(self, symbol: str, before_date: str, model: str, prompt_version: str = "") -> Optional[Dict]:
        """
        获取某只股票在指定日期之前的最近一次分析结果（用于 L1 记忆）。

        Args:
            symbol: 股票代码
            before_date: 截止日期（不包含该日期，格式：YYYY-MM-DD）
            model: LLM 模型名称
            prompt_version: prompt 版本（记忆只回溯同版本历史，避免旧版判断污染新链）

        Returns:
            最近一次分析结果字典，不存在则返回 None
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT symbol, analysis_date, model, direction, confidence,
                       reasons, sources, prompt_version
                FROM llm_analysis_cache
                WHERE symbol=? AND analysis_date<? AND model=? AND prompt_version=?
                ORDER BY analysis_date DESC
                LIMIT 1
            """, (symbol, before_date, model, prompt_version))
            
            row = cursor.fetchone()
            if row:
                return {
                    "symbol": row[0],
                    "analysis_date": row[1],
                    "model": row[2],
                    "direction": row[3],
                    "confidence": row[4],
                    "reasons": json.loads(row[5]) if row[5] else [],
                    "sources": json.loads(row[6]) if row[6] else [],
                    "prompt_version": row[7],
                }
            return None
    
    def query_by_symbol(self, symbol: str, limit: int = 100) -> List[Dict]:
        """
        按股票查询历史记录
        
        Args:
            symbol: 股票代码
            limit: 返回条数限制
        
        Returns:
            分析结果列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT symbol, analysis_date, model, direction, confidence
                FROM llm_analysis_cache
                WHERE symbol=?
                ORDER BY analysis_date DESC
                LIMIT ?
            """, (symbol, limit))
            
            return [
                {
                    "symbol": row[0],
                    "analysis_date": row[1],
                    "model": row[2],
                    "direction": row[3],
                    "confidence": row[4]
                }
                for row in cursor.fetchall()
            ]
    
    def query_by_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        """
        按日期范围查询
        
        Args:
            start_date: 起始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
        
        Returns:
            分析结果列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT symbol, analysis_date, model, direction, confidence
                FROM llm_analysis_cache
                WHERE analysis_date BETWEEN ? AND ?
                ORDER BY analysis_date DESC
            """, (start_date, end_date))
            
            return [
                {
                    "symbol": row[0],
                    "analysis_date": row[1],
                    "model": row[2],
                    "direction": row[3],
                    "confidence": row[4]
                }
                for row in cursor.fetchall()
            ]
    
    def statistics(self) -> List[Dict]:
        """
        统计信息（按股票分组）
        
        Returns:
            统计结果列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT 
                    symbol,
                    COUNT(*) as total,
                    AVG(confidence) as avg_confidence,
                    MIN(analysis_date) as first_date,
                    MAX(analysis_date) as last_date
                FROM llm_analysis_cache
                GROUP BY symbol
                ORDER BY total DESC
            """)
            
            return [
                {
                    "symbol": row[0],
                    "total": row[1],
                    "avg_confidence": row[2],
                    "first_date": row[3],
                    "last_date": row[4]
                }
                for row in cursor.fetchall()
            ]
    
    def count(self) -> int:
        """获取总缓存条数"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM llm_analysis_cache")
            return cursor.fetchone()[0]
    
    def clear_all(self):
        """清空所有缓存（谨慎使用）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM llm_analysis_cache")
            conn.commit()
            print("[LLM Cache DB] 已清空所有缓存")


# 全局实例（线程本地存储，每个线程独立连接 SQLite）
import threading

_local = threading.local()

def get_cache_db() -> LLMCacheDB:
    """获取当前线程的缓存数据库实例（线程安全）"""
    if not hasattr(_local, 'cache_db_instance') or _local.cache_db_instance is None:
        _local.cache_db_instance = LLMCacheDB()
    return _local.cache_db_instance
