"""清除 LLM 缓存"""
import sqlite3
from pathlib import Path

db_path = Path("data/cache/llm/llm_cache.db")
if not db_path.exists():
    print(f"数据库不存在：{db_path}")
    exit()

conn = sqlite3.connect(str(db_path))
count_before = conn.execute("SELECT COUNT(*) FROM llm_analysis_cache").fetchone()[0]
print(f"当前缓存数量：{count_before}")

conn.execute("DELETE FROM llm_analysis_cache")
conn.commit()

count_after = conn.execute("SELECT COUNT(*) FROM llm_analysis_cache").fetchone()[0]
print(f"已清除 {count_before - count_after} 条缓存")
conn.close()
