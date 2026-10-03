"""退役中的旧回测基线数据层（core.legacy）

这里存放主线（fetch -> raw -> EvidenceCard -> Agent）不再使用的旧采集/分析模块：
  - news_data / market_data: CSV 网格缓存 + DataFrame 供数，服务旧基线
    （run_backtest 的 LLM/VADER 对照、PPO 训练数据加载、data_fetch_tracker）
  - sentiment: VADER 情绪打分

治理决定（2026-10，见 docs/development_plan.md 阶段 1）：
  主线一律改用 core/data/ 新采集器；本目录只维持旧基线可运行，
  阶段 6 基线对照退役时整个目录连同 data/cache/ 旧缓存一起删除。
  禁止在本目录新增功能，也禁止主线代码 import 本目录。
"""
