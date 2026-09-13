"""
PPO 仓位融合器模块（core/ppo）—— 阶段 6 从零重建。

两阶段训练（详见 docs/Plan_v2.md 阶段 6）：
  - 阶段一：纯 13 维数值特征预训练通用仓位择时（标准 PPO）
  - 阶段二：冻结主干 + 5 维 LLM 旁路网络微调（ResidualLLMPolicy）

子模块：
  - features: 13 numeric + 5 llm 特征工程（纯函数）
  - env:      gymnasium 环境（Dict obs + 差分夏普奖励 + 组合执行）
  - policy:   NumericExtractor + ResidualLLMPolicy（双旁路 + zero-init）
  - trainer:  阶段一预训练 / 阶段二微调
  - ablation: LLM 边际贡献量化

此处不在包级 import 子模块，避免子模块间循环依赖；按需 `from core.ppo.features import ...`。
"""
