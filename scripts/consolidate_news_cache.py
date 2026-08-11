"""
新闻分段缓存归并/重整脚本（Plan.md 假设 4 配套）

背景：
  历史上 `_fetch_news_segmented_av` 的段边界随请求起点漂移，缓存目录里
  积累了多套重叠的分段文件（如 30 天段与 14 天段并存），同一批新闻被重复
  拉取，浪费 Alpha Vantage 配额。

本脚本做的事（纯本地，不访问网络）：
  1. 按 symbol 收集所有 `*_news_seg_*.csv` 分段缓存
  2. 合并去重（按 title 保留首条，与 fetch_news 一致）
  3. 按 config.NEWS_SEGMENT_ANCHOR 定义的 14 天全局网格，把每条新闻分到其网格段
  4. 把原有分段文件**移动**到归档目录（可逆，不删除），再写出干净的网格段文件

用法：
  python -m scripts.consolidate_news_cache            # 处理全部 symbol
  python -m scripts.consolidate_news_cache --symbol AAPL NVDA
  python -m scripts.consolidate_news_cache --dry-run  # 只打印计划，不落盘
"""

import sys
import re
import shutil
import argparse
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd

import config
from core.data.news_data import _segment_bounds_for_grid, _try_load_cache

_SEG_PATTERN = re.compile(r"^([A-Z.]+)_news_seg_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$")


def _discover_seg_files(news_dir: Path):
    """返回 {symbol: [Path, ...]}，仅包含 *_news_seg_*.csv。"""
    by_symbol = {}
    for f in news_dir.glob("*_news_seg_*.csv"):
        m = _SEG_PATTERN.match(f.name)
        if not m:
            continue
        symbol = m.group(1)
        by_symbol.setdefault(symbol, []).append(f)
    return by_symbol


def _naive_ts(value) -> pd.Timestamp:
    """转成 tz-naive 的 Timestamp（网格计算要求 tz 一致）。"""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def consolidate_symbol(symbol: str, files, news_dir: Path, archive_dir: Path,
                       anchor: pd.Timestamp, seg_days: int, dry_run: bool) -> dict:
    """归并单个 symbol 的所有分段缓存到网格。返回统计信息。"""
    # 1. 合并所有分段
    dfs = []
    for f in files:
        df = _try_load_cache(f)
        if df is not None and len(df) > 0:
            if "symbol" not in df.columns:
                df["symbol"] = symbol
            dfs.append(df)

    if not dfs:
        return {"symbol": symbol, "files_in": len(files), "rows": 0, "cells_out": 0}

    merged = pd.concat(dfs, ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["title"], keep="first")
    merged = merged.dropna(subset=["datetime"])
    merged = merged.sort_values("datetime").reset_index(drop=True)
    after = len(merged)

    # 2. 每条新闻分配到网格段
    cell_starts = []
    for dt in merged["datetime"]:
        seg_start, _ = _segment_bounds_for_grid(_naive_ts(dt), anchor, seg_days)
        cell_starts.append(seg_start)
    merged["_cell_start"] = cell_starts

    # 3. 归档原分段文件（移动，不删除）
    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.move(str(f), str(archive_dir / f.name))

    # 4. 按网格段写出干净文件到 complete 目录
    complete_dir = config.NEWS_CACHE_COMPLETE_DIR
    cells_out = 0
    for cell_start, group in merged.groupby("_cell_start"):
        cell_end = cell_start + pd.Timedelta(days=seg_days - 1)
        out_name = (f"{symbol}_news_seg_"
                    f"{cell_start.strftime('%Y-%m-%d')}_{cell_end.strftime('%Y-%m-%d')}.csv")
        out_path = complete_dir / out_name
        out_df = group.drop(columns=["_cell_start"])
        if not dry_run:
            out_df.to_csv(out_path, index=False)
        cells_out += 1

    return {
        "symbol": symbol,
        "files_in": len(files),
        "rows": after,
        "dedup_removed": before - after,
        "cells_out": cells_out,
    }


def main():
    parser = argparse.ArgumentParser(description="新闻分段缓存归并到固定网格")
    parser.add_argument("--symbol", nargs="+", default=None, help="只处理指定 symbol（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = parser.parse_args()

    news_dir = config.NEWS_CACHE_DIR
    archive_dir = news_dir / "_archive_pre_grid"
    anchor = pd.Timestamp(config.NEWS_SEGMENT_ANCHOR)
    seg_days = config.NEWS_SEGMENT_DAYS

    print("=" * 60)
    print("  新闻分段缓存归并到固定网格")
    print("=" * 60)
    print(f"  缓存目录：{news_dir}")
    print(f"  归档目录：{archive_dir}")
    print(f"  网格锚点：{anchor.strftime('%Y-%m-%d')}，段长：{seg_days} 天")
    print(f"  模式：{'DRY-RUN（不落盘）' if args.dry_run else '执行'}")

    by_symbol = _discover_seg_files(news_dir)
    if args.symbol:
        wanted = set(args.symbol)
        by_symbol = {s: fs for s, fs in by_symbol.items() if s in wanted}

    if not by_symbol:
        print("\n  未发现任何分段缓存文件，无需处理。")
        return

    print(f"\n  发现 {len(by_symbol)} 个 symbol 的分段缓存")
    results = []
    for symbol in sorted(by_symbol):
        stats = consolidate_symbol(
            symbol, by_symbol[symbol], news_dir, archive_dir,
            anchor, seg_days, args.dry_run,
        )
        results.append(stats)
        print(f"    {symbol}: {stats['files_in']} 段文件 → "
              f"{stats.get('cells_out', 0)} 网格段，"
              f"{stats.get('rows', 0)} 条（去重 -{stats.get('dedup_removed', 0)}）")

    total_in = sum(r["files_in"] for r in results)
    total_out = sum(r.get("cells_out", 0) for r in results)
    print("\n" + "=" * 60)
    print(f"  完成：{total_in} 个旧分段文件 → {total_out} 个网格段文件")
    if not args.dry_run:
        print(f"  旧文件已归档到：{archive_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
