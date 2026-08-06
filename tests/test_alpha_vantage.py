"""测试 Alpha Vantage 历史新闻 API"""

from core.data.news_data import fetch_news

print("测试 Alpha Vantage 历史新闻 API...")
print("=" * 60)

# 测试 2024 年 1 月的新闻（Finnhub 不支持的历史数据）
df = fetch_news("AAPL", "2024-01-01", "2024-01-31")

print(f"\n新闻数量：{len(df)}")

if len(df) > 0:
    print("\n前 5 条新闻：")
    print(df.head(5)[["datetime", "title", "source"]].to_string())
    
    # 检查是否有真实新闻
    real_news = df[df["title"] != "No news available"]
    print(f"\n真实新闻数量：{len(real_news)} / {len(df)}")
    
    if len(real_news) > 0:
        print("\n前 3 条真实新闻详情：")
        for i, row in real_news.head(3).iterrows():
            print(f"\n[{i}] {row['datetime']}")
            print(f"    标题：{row['title']}")
            print(f"    来源：{row['source']}")
            print(f"    情绪分数：{row['sentiment_score']:.3f}")
            print(f"    摘要：{str(row['summary'])[:100]}...")
else:
    print("\n没有获取到新闻数据")
