"""
新闻数据拉取进度追踪器

功能：
  1. 扫描 data/cache/news 目录，统计每只股票的 segment 覆盖情况
  2. 生成进度表（终端表格 + JSON 文件）
  3. 按每日配额（AV 25次/天）拉取缺失的 segment

用法：
  python scripts/data_fetch_tracker.py status          # 查看进度表
  python scripts/data_fetch_tracker.py fetch            # 拉取今日配额内的缺失段
  python scripts/data_fetch_tracker.py fetch --limit 10 # 指定本次拉取次数上限
  python scripts/data_fetch_tracker.py reset            # 重置进度（不删缓存文件）
"""

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Windows 终端 UTF-8 输出
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config

# ==================== 配置 ====================

# 目标股票池（10 只）
STOCKS = ["AAPL", "AMZN", "GOOGL", "JNJ", "JPM", "MSFT", "NVDA", "UNH", "V", "WMT"]

# 目标日期范围：近一年
TARGET_START = "2025-08-11"
TARGET_END = "2026-08-06"

# 每段覆盖天数（与 config.NEWS_SEGMENT_DAYS 保持一致）
SEGMENT_DAYS = 14

# 进度文件路径
PROGRESS_FILE = config.CACHE_DIR / "data_fetch_progress.json"


# ==================== 段计算 ====================

def compute_segments(start_date: str, end_date: str, segment_days: int) -> List[Tuple[str, str]]:
    """
    计算覆盖 [start_date, end_date] 所需的全部段。

    返回: [(seg_start, seg_end), ...]
    """
    segments = []
    current = __import__("pandas").Timestamp(start_date)
    end = __import__("pandas").Timestamp(end_date)

    while current <= end:
        seg_end = min(current + timedelta(days=segment_days - 1), end)
        segments.append((current.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        current = seg_end + timedelta(days=1)

    return segments


def get_existing_segments(symbol: str) -> Dict[Tuple[str, str], Path]:
    """
    扫描缓存目录，获取某只股票已有的 segment 文件。

    返回: {(seg_start, seg_end): cache_path}
    """
    pattern = re.compile(
        rf"^{symbol}_news_seg_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})\.csv$"
    )
    result = {}
    for f in config.NEWS_CACHE_DIR.glob(f"{symbol}_news_seg_*.csv"):
        m = pattern.match(f.name)
        if m:
            result[(m.group(1), m.group(2))] = f
    return result


def is_segment_covered(
    seg_start: str, seg_end: str,
    existing: Dict[Tuple[str, str], Path],
) -> bool:
    """
    检查标准段 [seg_start, seg_end] 是否已被某个已有 segment 完全覆盖。

    支持精确匹配和宽范围覆盖（如旧的月度段覆盖标准 14 天段）。
    """
    import pandas as pd

    req_s = pd.Timestamp(seg_start)
    req_e = pd.Timestamp(seg_end)

    for (ex_s, ex_e), _ in existing.items():
        ex_start = pd.Timestamp(ex_s)
        ex_end = pd.Timestamp(ex_e)
        if ex_start <= req_s and ex_end >= req_e:
            return True
    return False


def get_full_range_cache(symbol: str) -> Optional[Path]:
    """检查是否存在覆盖全范围的主缓存文件。"""
    path = config.NEWS_CACHE_DIR / f"{symbol}_news_{TARGET_START}_{TARGET_END}.csv"
    return path if path.exists() else None


# ==================== 进度分析 ====================

def analyze_progress() -> Dict:
    """
    分析所有股票的缓存覆盖情况。

    返回结构化的进度信息。
    """
    all_segments = compute_segments(TARGET_START, TARGET_END, SEGMENT_DAYS)
    total_segments = len(all_segments)

    stock_status = {}
    total_cached = 0
    total_needed = 0

    for symbol in STOCKS:
        existing = get_existing_segments(symbol)
        full_cache = get_full_range_cache(symbol)

        # 计算缺失段（支持重叠覆盖检测）
        missing = []
        cached_segs = []
        for seg_start, seg_end in all_segments:
            if (seg_start, seg_end) in existing or is_segment_covered(seg_start, seg_end, existing):
                cached_segs.append((seg_start, seg_end))
            else:
                missing.append((seg_start, seg_end))

        cached_count = len(cached_segs)
        missing_count = len(missing)
        total_cached += cached_count
        total_needed += total_segments

        # 计算覆盖的连续天数
        covered_days = cached_count * SEGMENT_DAYS
        # 最后一段可能不足 SEGMENT_DAYS 天
        if cached_segs:
            last_seg_end = __import__("pandas").Timestamp(cached_segs[-1][1])
            first_seg_start = __import__("pandas").Timestamp(cached_segs[0][0])
            # 实际覆盖天数（从第一段开始到最后一段结束）
            span_days = (last_seg_end - first_seg_start).days + 1
        else:
            span_days = 0

        stock_status[symbol] = {
            "cached_segments": cached_segs,
            "missing_segments": missing,
            "cached_count": cached_count,
            "missing_count": missing_count,
            "total_segments": total_segments,
            "progress_pct": round(cached_count / total_segments * 100, 1) if total_segments > 0 else 0,
            "full_range_cache": full_cache is not None,
            "has_gaps": len(cached_segs) > 0 and missing_count > 0,
        }

    return {
        "target_start": TARGET_START,
        "target_end": TARGET_END,
        "segment_days": SEGMENT_DAYS,
        "total_segments_per_stock": total_segments,
        "total_stocks": len(STOCKS),
        "stocks": stock_status,
        "summary": {
            "total_segments_needed": total_needed,
            "total_segments_cached": total_cached,
            "total_segments_missing": total_needed - total_cached,
            "overall_pct": round(total_cached / total_needed * 100, 1) if total_needed > 0 else 0,
            "estimated_days_remaining": 0,
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==================== 终端展示 ====================

def print_progress_table(progress: Dict):
    """在终端打印进度表。"""
    stocks = progress["stocks"]
    summary = progress["summary"]

    print()
    print("=" * 90)
    print(f"  新闻数据拉取进度表    目标范围: {progress['target_start']} ~ {progress['target_end']}")
    print(f"  每股票需 {progress['total_segments_per_stock']} 段 x {progress['segment_days']}天/段  |  "
          f"共 {progress['total_stocks']} 只股票")
    print("=" * 90)

    # 表头
    header = f"{'股票':<8} {'已缓存':>6} {'缺失':>6} {'总计':>6} {'进度':>8} {'状态':<12} {'缺失段详情'}"
    print(header)
    print("-" * 90)

    for symbol in STOCKS:
        info = stocks[symbol]
        cached = info["cached_count"]
        missing = info["missing_count"]
        total = info["total_segments"]
        pct = info["progress_pct"]

        # 状态判断
        if missing == 0:
            status = "✓ 完成"
        elif cached == 0:
            status = "✗ 未开始"
        elif info["has_gaps"]:
            status = "◐ 有间隔"
        else:
            status = "◑ 进行中"

        # 缺失段摘要（最多显示 3 段）
        missing_strs = []
        for seg_s, seg_e in info["missing_segments"][:3]:
            missing_strs.append(f"{seg_s}~{seg_e}")
        if len(info["missing_segments"]) > 3:
            missing_strs.append(f"...+{len(info['missing_segments'])-3}段")
        missing_detail = ", ".join(missing_strs) if missing_strs else "-"

        bar_len = int(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)

        print(f"{symbol:<8} {cached:>6} {missing:>6} {total:>6} "
              f"[{bar}] {pct:>5.1f}%  {status:<12} {missing_detail}")

    print("-" * 90)
    print(f"  总计: {summary['total_segments_cached']}/{summary['total_segments_needed']} 段  "
          f"({summary['overall_pct']}%)  |  "
          f"剩余 {summary['total_segments_missing']} 段  ~ "
          f"{summary['estimated_days_remaining']} 天 (25次/天)")
    print("=" * 90)
    print()


# ==================== 拉取逻辑 ====================

def fetch_missing_segments(progress: Dict, limit: int = 25) -> int:
    """
    按配额拉取缺失的 segment。

    策略：轮流拉取各股票的缺失段（round-robin），避免某只股票拉不完。
    返回实际拉取次数。
    """
    # 导入拉取函数
    from core.data.news_data import _try_alpha_vantage_news, _save_cache

    stocks_with_missing = [s for s in STOCKS if progress["stocks"][s]["missing_count"] > 0]
    if not stocks_with_missing:
        print("✓ 所有股票的新闻数据已完整，无需拉取。")
        return 0

    quota_remaining = limit
    total_fetched = 0
    round_num = 0

    while quota_remaining > 0:
        # 检查是否还有缺失段
        any_missing = False
        for symbol in stocks_with_missing:
            if progress["stocks"][symbol]["missing_count"] > 0:
                any_missing = True
                break
        if not any_missing:
            break

        round_num += 1
        print(f"\n--- 第 {round_num} 轮拉取 ---")

        for symbol in stocks_with_missing:
            if quota_remaining <= 0:
                break

            info = progress["stocks"][symbol]
            if info["missing_count"] == 0:
                continue

            # 取第一个缺失段
            seg_start, seg_end = info["missing_segments"][0]
            seg_cache = config.NEWS_CACHE_DIR / f"{symbol}_news_seg_{seg_start}_{seg_end}.csv"

            print(f"  [{symbol}] 拉取 {seg_start} ~ {seg_end} ...", end=" ", flush=True)

            df = _try_alpha_vantage_news(symbol, seg_start, seg_end)
            quota_remaining -= 1
            total_fetched += 1

            if df is not None and len(df) > 0:
                df["symbol"] = symbol
                _save_cache(df, seg_cache)
                print(f"✓ {len(df)} 条")

                # 更新进度
                info["missing_segments"].pop(0)
                info["cached_segments"].append((seg_start, seg_end))
                info["cached_count"] += 1
                info["missing_count"] -= 1
                info["progress_pct"] = round(
                    info["cached_count"] / info["total_segments"] * 100, 1
                )
            else:
                print("✗ 无数据或请求失败")
                # 即使失败也消耗配额，但段保留在 missing 中

            # 限流保护：5 次/分钟
            if quota_remaining > 0:
                time.sleep(15)

    # 更新汇总统计
    _update_summary(progress)

    print(f"\n本次共拉取 {total_fetched} 段，消耗 {total_fetched} 次 API 配额。")
    return total_fetched


def _update_summary(progress: Dict):
    """重新计算汇总统计。"""
    total_cached = sum(info["cached_count"] for info in progress["stocks"].values())
    total_needed = progress["summary"]["total_segments_needed"]
    progress["summary"]["total_segments_cached"] = total_cached
    progress["summary"]["total_segments_missing"] = total_needed - total_cached
    progress["summary"]["overall_pct"] = round(total_cached / total_needed * 100, 1) if total_needed > 0 else 0
    progress["summary"]["estimated_days_remaining"] = (
        -(-progress["summary"]["total_segments_missing"] // 25)  # ceil division
    )
    progress["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==================== 进度文件持久化 ====================

def save_progress(progress: Dict):
    """保存进度到 JSON 文件。"""
    # 将 tuple 转为 list（JSON 兼容）
    serializable = json.loads(json.dumps(progress, default=str))
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)
    print(f"进度已保存到 {PROGRESS_FILE}")


def load_progress() -> Optional[Dict]:
    """从 JSON 文件加载进度（如果存在）。"""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="新闻数据拉取进度追踪器")
    parser.add_argument("command", choices=["status", "fetch", "reset"],
                        help="status=查看进度, fetch=拉取缺失段, reset=重置进度")
    parser.add_argument("--limit", type=int, default=25,
                        help="fetch 模式下的 API 调用次数上限（默认 25）")
    parser.add_argument("--json", action="store_true",
                        help="status 模式下输出 JSON 格式")

    args = parser.parse_args()

    if args.command == "status":
        progress = analyze_progress()
        _update_summary(progress)
        save_progress(progress)

        if args.json:
            print(json.dumps(progress, indent=2, ensure_ascii=False, default=str))
        else:
            print_progress_table(progress)

    elif args.command == "fetch":
        progress = analyze_progress()
        _update_summary(progress)

        print_progress_table(progress)
        fetched = fetch_missing_segments(progress, limit=args.limit)
        save_progress(progress)

        # 拉取后重新打印进度
        if fetched > 0:
            print_progress_table(progress)

    elif args.command == "reset":
        # 只重置进度文件，不删除缓存
        progress = analyze_progress()
        _update_summary(progress)
        save_progress(progress)
        print("进度已重新扫描并保存。（缓存文件未删除）")


if __name__ == "__main__":
    main()
