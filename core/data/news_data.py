"""
新闻数据采集模块（NewsCollector）

职责：
  1. 从 Finnhub/Alpha Vantage 获取历史新闻数据
  2. 按 (symbol, date) 存储到本地 CSV
  3. 保证时点正确性（Point-in-Time）：t 时刻只能用 t 之前发布的新闻

设计说明（三层兜底）：
  - 本地缓存：最快，有缓存就不走网络
  - Alpha Vantage：优先数据源（历史覆盖广，自带情绪分，25 次/天）
  - Finnhub：兜底数据源（AV 失败时使用，60 次/天）

关键约束（防未来函数）：
  - 新闻必须记录**发布时间戳**（不能只用日期）
  - 回测时 t 时刻只能使用 datetime <= t 的新闻
  - 不能用事后修订版新闻
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

import pandas as pd
import requests

from .. import config


# ==================== 公开接口 ====================

def fetch_news(
    symbol: str = config.DEFAULT_SYMBOL,
    start_date: str = config.DEFAULT_START_DATE,
    end_date: str = config.DEFAULT_END_DATE,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    获取指定标的的历史新闻数据。
    
    参数：
      symbol: 股票代码（如 "AAPL"）
      start_date: 起始日期（格式 "YYYY-MM-DD"）
      end_date: 结束日期（格式 "YYYY-MM-DD"）
      use_cache: 是否使用本地缓存（默认 True）
    
    返回：
      pd.DataFrame，列 = [datetime, title, summary, source, sentiment_score]
      - datetime: 新闻发布时间（pd.Timestamp）
      - title: 新闻标题
      - summary: 新闻摘要
      - source: 新闻来源
      - sentiment_score: 情绪分数（-1.0 ~ 1.0，正=看涨，负=看跌）
    
    数据源策略（优先 + 兜底）：
      1. 本地 CSV 缓存（有缓存就直接用）
      2. Alpha Vantage（历史覆盖广，自带情绪分，优先）
      3. Finnhub（AV 失败时的兜底）
    """
    # 生成缓存文件名
    cache_filename = f"{symbol}_news_{start_date}_{end_date}.csv"
    cache_path = config.CACHE_DIR / cache_filename
    
    # --- 第 1 层：检查本地缓存 ---
    if use_cache:
        df = _try_load_cache(cache_path)
        if df is not None:
            print(f"[NewsCollector] 命中本地缓存：{cache_filename}")
            return df
    
    # --- 第 2 层：Alpha Vantage 优先（历史覆盖广 + 自带情绪分）---
    if config.NEWS_MULTI_SEGMENT:
        # 多段模式：切分日期范围，逐段请求后合并（PPO 训练用）
        df = _fetch_news_segmented_av(symbol, start_date, end_date)
    else:
        # 单段模式：一次请求（阶段 1-3 足够）
        df = _try_alpha_vantage_news(symbol, start_date, end_date)

    if df is not None and len(df) > 0:
        print(f"[NewsCollector] Alpha Vantage 获取成功：{symbol}, {len(df)} 条新闻")
        df["symbol"] = symbol
        _save_cache(df, cache_path)
        return df
    
    # --- 第 3 层：Finnhub 兜底 ---
    df = _try_finnhub(symbol, start_date, end_date)
    if df is not None and len(df) > 0:
        print(f"[NewsCollector] Finnhub 获取成功：{symbol}, {len(df)} 条新闻")
        df["symbol"] = symbol
        _save_cache(df, cache_path)
        return df
    
    # --- 所有数据源均失败：返回空 DataFrame（不缓存，下次重试）---
    print(f"[NewsCollector] 所有新闻源均不可用，返回空 DataFrame：{symbol}")
    df = pd.DataFrame(columns=["datetime", "title", "summary", "source", "sentiment_score", "symbol"])
    return df


def get_news_at_date(
    news_df: pd.DataFrame,
    symbol: str,
    target_date: str,
) -> List[Dict]:
    """
    获取指定日期的新闻列表（严格时点对齐）。
    
    参数：
      news_df: 新闻 DataFrame（由 fetch_news 返回）
      symbol: 股票代码
      target_date: 目标日期（格式 "YYYY-MM-DD"）
    
    返回：
      List[Dict]，每个 dict 包含：
        {
          "datetime": pd.Timestamp,
          "title": str,
          "summary": str,
          "source": str,
          "sentiment_score": float
        }
    
    时点对齐规则：
      - 只返回 datetime <= target_date 23:59:59 的新闻
      - 不能用 target_date 之后的新闻（即使有）
    """
    # 去除时区信息，只保留日期部分
    target_date_str = str(target_date).split(' ')[0]  # 取 "YYYY-MM-DD" 部分
    target_dt = pd.Timestamp(target_date_str) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    
    # 处理时区问题：确保 target_dt 和 news_df["datetime"] 时区一致
    news_dt_dtype = news_df["datetime"].dtype
    if hasattr(news_dt_dtype, 'tz'):
        # pandas datetime with timezone
        if news_dt_dtype.tz is not None:
            # news_df 有时区，给 target_dt 也加上
            target_dt = target_dt.tz_localize(news_dt_dtype.tz)
    # 否则 news_df 是 tz-naive，target_dt 也保持 tz-naive
    
    # 过滤出目标日期及之前的新闻
    mask = (news_df["datetime"] <= target_dt)
    filtered = news_df[mask]
    
    # 转换为列表格式
    news_list = []
    for _, row in filtered.iterrows():
        news_list.append({
            "datetime": row["datetime"],
            "title": row["title"],
            "summary": row["summary"],
            "source": row["source"],
            "sentiment_score": row["sentiment_score"],
        })
    
    return news_list


# ==================== 各数据源实现 ====================

def _try_load_cache(cache_path: Path) -> Optional[pd.DataFrame]:
    """尝试从本地 CSV 读取缓存数据。"""
    if not cache_path.exists():
        return None
    
    try:
        df = pd.read_csv(cache_path, parse_dates=["datetime"])
        required_cols = {"datetime", "title", "summary", "source", "sentiment_score"}
        if not required_cols.issubset(df.columns):
            print(f"[NewsCollector] 缓存文件列不完整，跳过：{cache_path}")
            return None
        return df
    except Exception as e:
        print(f"[NewsCollector] 读取缓存失败：{e}")
        return None


def _try_finnhub(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """
    通过 Finnhub News API 获取新闻数据。
    
    免费版限制：60 次/天
    API 文档：https://finnhub.io/docs/api/company-news
    """
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        print("[NewsCollector] Finnhub API Key 未配置，跳过")
        return None
    
    try:
        # Finnhub API 只支持最近 1 个月的新闻，历史数据需要付费
        # 这里先尝试获取最近的数据
        from_date = pd.Timestamp(start_date)
        to_date = pd.Timestamp(end_date)
        
        # 如果时间范围超过 1 个月，分段获取
        all_news = []
        current_from = from_date
        
        while current_from < to_date:
            current_to = min(current_from + pd.Timedelta(days=30), to_date)
            
            url = (
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={symbol}"
                f"&from={current_from.strftime('%Y-%m-%d')}"
                f"&to={current_to.strftime('%Y-%m-%d')}"
                f"&token={api_key}"
            )
            
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                print(f"[NewsCollector] Finnhub 返回空数据：{symbol} ({current_from} → {current_to})")
                current_from = current_to
                continue
            
            # 解析新闻
            for item in data:
                all_news.append({
                    "datetime": pd.Timestamp(item["datetime"], unit="s"),
                    "title": item.get("headline", ""),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", "Finnhub"),
                    "sentiment_score": 0.0,  # Finnhub 免费版不提供情绪分数，需要自己算
                })
            
            # 避免触发限流
            time.sleep(1)
            current_from = current_to
        
        if not all_news:
            return None
        
        df = pd.DataFrame(all_news)
        df = df.sort_values("datetime").reset_index(drop=True)
        
        return df
    
    except Exception as e:
        print(f"[NewsCollector] Finnhub 获取失败：{e}")
        return None


def _fetch_news_segmented_av(
    symbol: str,
    start_date: str,
    end_date: str,
) -> Optional[pd.DataFrame]:
    """
    多段请求 Alpha Vantage News，拼接更长的历史覆盖。

    将 [start_date, end_date] 按 NEWS_SEGMENT_DAYS 天切分为多段，
    每段独立调用 AV API（单次最多 1000 条），最后合并去重。

    每段独立缓存：已拉取的段不会重复请求，可以分多天完成。
    免费版 25 次/天，3 年数据（~78 段 × 2 股）需要 ~6 天分批拉取。
    """
    segment_days = config.NEWS_SEGMENT_DAYS
    seg_start = pd.Timestamp(start_date)
    seg_end_full = pd.Timestamp(end_date)

    all_dfs = []
    seg_idx = 0
    skipped_cache = 0
    fetched_new = 0
    quota_remaining = config.NEWS_DAILY_QUOTA  # 本次运行的 API 配额
    quota_exhausted = False

    while seg_start < seg_end_full:
        chunk_end = min(seg_start + pd.Timedelta(days=segment_days - 1), seg_end_full)
        seg_idx += 1

        seg_start_str = seg_start.strftime('%Y-%m-%d')
        seg_end_str = chunk_end.strftime('%Y-%m-%d')

        # 每段独立缓存
        seg_cache = config.CACHE_DIR / f"{symbol}_news_seg_{seg_start_str}_{seg_end_str}.csv"
        cached = _try_load_cache(seg_cache)

        if cached is not None:
            all_dfs.append(cached)
            skipped_cache += 1
        elif quota_remaining > 0:
            print(f"  [NewsCollector] 分段请求 [{seg_idx}]：{seg_start_str} ~ {seg_end_str}")
            chunk_df = _try_alpha_vantage_news(symbol, seg_start_str, seg_end_str)
            quota_remaining -= 1

            if chunk_df is not None and len(chunk_df) > 0:
                chunk_df["symbol"] = symbol
                _save_cache(chunk_df, seg_cache)
                all_dfs.append(chunk_df)
                fetched_new += 1

            # 限流保护：免费版 5 次/分钟，留余量
            if seg_start + pd.Timedelta(days=segment_days) < seg_end_full and quota_remaining > 0:
                time.sleep(13)
        else:
            if not quota_exhausted:
                print(f"  [NewsCollector] 本次配额已用完 "
                      f"({config.NEWS_DAILY_QUOTA} 次)，剩余段下次再拉")
                quota_exhausted = True

        seg_start = chunk_end + pd.Timedelta(days=1)

    if not all_dfs:
        return None

    df = pd.concat(all_dfs, ignore_index=True)
    before = len(df)
    df = df.drop_duplicates(subset=["title"], keep="first")
    df = df.sort_values("datetime").reset_index(drop=True)
    after = len(df)

    print(f"  [NewsCollector] 分段合并：{seg_idx} 段 "
          f"(缓存命中 {skipped_cache}, 新拉取 {fetched_new}), "
          f"{before} → {after} 条")

    return df


def _try_alpha_vantage_news(symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """
    通过 Alpha Vantage News & Sentiment API 获取新闻数据。
    
    免费版限制：25 次/天
    API 文档：https://www.alphavantage.co/documentation/#news-sentiment
    """
    api_key = config.ALPHA_VANTAGE_API_KEY
    if not api_key:
        print("[NewsCollector] Alpha Vantage API Key 未配置，跳过")
        return None
    
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=NEWS_SENTIMENT"
            f"&tickers={symbol}"
            f"&time_from={pd.Timestamp(start_date).strftime('%Y%m%dT0000')}"
            f"&time_to={pd.Timestamp(end_date).strftime('%Y%m%dT2359')}"
            f"&limit=1000"
            f"&apikey={api_key}"
        )
        
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if "feed" not in data:
            print(f"[NewsCollector] Alpha Vantage 返回格式异常：{list(data.keys())}")
            return None
        
        feed = data["feed"]
        if not feed:
            return None
        
        # 解析新闻
        all_news = []
        for item in feed:
            # Alpha Vantage 提供情绪分数
            sentiment_score = 0.0
            if "overall_sentiment_score" in item:
                sentiment_score = float(item["overall_sentiment_score"])
            
            all_news.append({
                "datetime": pd.to_datetime(item["time_published"], format="%Y%m%dT%H%M%S"),
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "source": item.get("source", "Alpha Vantage"),
                "sentiment_score": sentiment_score,
            })
        
        df = pd.DataFrame(all_news)
        df = df.sort_values("datetime").reset_index(drop=True)
        
        return df
    
    except Exception as e:
        print(f"[NewsCollector] Alpha Vantage 获取失败：{e}")
        return None


# ==================== 缓存写入 ====================

def _save_cache(df: pd.DataFrame, cache_path: Path) -> None:
    """将 DataFrame 保存为 CSV 缓存。"""
    try:
        df.to_csv(cache_path, index=False)
        print(f"[NewsCollector] 已缓存到：{cache_path.name}")
    except Exception as e:
        print(f"[NewsCollector] 缓存写入失败（不影响本次使用）: {e}")


# ==================== 测试入口 ====================

if __name__ == "__main__":
    # 测试获取新闻
    print("测试获取 AAPL 新闻数据...")
    news_df = fetch_news("AAPL", "2023-01-01", "2023-12-31")
    print(f"获取到 {len(news_df)} 条新闻")
    print(news_df.head(10))
    
    # 测试获取某日新闻
    print("\n测试获取 2023-06-15 的新闻...")
    news_list = get_news_at_date(news_df, "AAPL", "2023-06-15")
    print(f"获取到 {len(news_list)} 条新闻")
    for news in news_list[:3]:
        print(f"  - {news['datetime']}: {news['title'][:50]}...")
