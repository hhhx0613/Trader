"""测试 Finnhub API - 最近时间段"""

from core.data.news_data import fetch_news
from datetime import datetime, timedelta

print("测试 Finnhub API（最近 1 个月）...")
print("=" * 60)

# 测试最近 1 个月的新闻（Finnhub 免费版只支持最近 1 个月）
end_date = datetime.now()
start_date = end_date - timedelta(days=30)

start_str = start_date.strftime("%Y-%m-%d")
end_str = end_date.strftime("%Y-%m-%d")

print(f"\n查询时间范围：{start_str} ~ {end_str}")

df = fetch_news("AAPL", start_str, end_str)

print(f"\n新闻数量：{len(df)}")

if len(df) > 0:
    print("\n前 5 条新闻：")
    print(df.head(5)[["datetime", "title"]].to_string())
    
    # 检查是否有真实新闻
    real_news = df[df["title"] != "No news available"]
    print(f"\n真实新闻数量：{len(real_news)} / {len(df)}")
    
    if len(real_news) > 0:
        print("\n前 3 条真实新闻：")
        print(real_news.head(3)[["datetime", "title", "summary"]].to_string())
else:
    print("\n没有获取到新闻数据")
