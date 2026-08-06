"""测试 Finnhub API"""

from core.data.news_data import fetch_news

print("测试 Finnhub API...")
print("=" * 60)

# 测试 2024 年 1 月的新闻
df = fetch_news("AAPL", "2024-01-01", "2024-01-31")

print(f"\n新闻数量：{len(df)}")

if len(df) > 0:
    print("\n前 5 条新闻：")
    print(df.head(5)[["datetime", "title", "summary"]].to_string())
    
    # 检查是否有真实新闻（不是 "No news available"）
    real_news = df[df["title"] != "No news available"]
    print(f"\n真实新闻数量：{len(real_news)} / {len(df)}")
else:
    print("\n没有获取到新闻数据")
