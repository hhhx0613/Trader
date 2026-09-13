"""
PPO 择时预测器（predict）

把训练好的 stage1 模型接进真实回测引擎，只接管「总暴露」这一个旋钮：
选股（LLM Top-K）与个股权重（逆波动率）保持 decision_func 原样不动。

职责边界（SOLID）：
  - 本模块只做「推理」：加载模型 → 拼 numeric(13) → 贪心动作 → 映射暴露档位。
  - 账户状态（现金/持仓/净值）由 engine 重建后传入；训练编排在 trainer.py；特征在 features.py。

为什么单独成模块（而非塞进 engine 或 decision_func）：
  1. decision_func 拿不到运行时账户状态，无法重建 numeric 的账户 3 维；
  2. engine 有账户状态，但不应耦合 SB3 模型加载与行情预热细节。
  故：engine 负责算账户状态 → 本模块负责「特征拼装 + 模型推理 + 档位映射」。
"""

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from stable_baselines3 import PPO

from core import config
from core.ppo.data import load_market_data
from core.ppo.env import EXPOSURE_LEVELS
from core.ppo.features import build_numeric_obs, zeros_llm

# 模型默认路径（与 trainer._MODEL_DIR 一致）
_MODEL_DIR = config.PROJECT_ROOT / "models"

# 特征回看缓冲：为让回测「首日」也有完整的 _LOOKBACK(60) 交易日窗口，
# 预测器自持的行情比回测起点多回扩这么多日历天（60 交易日 ≈ 84 日历日，取 120 留余量）。
# 为什么需要：run_backtest 把引擎行情裁回了 start_date，而 env 训练/评估从 index≥60
# 才开始决策（前 60 天充当预热）。若直接用裁剪后的行情，回测前 ~60 交易日的长窗口特征
# （regime/相关性/中期收益）会因历史不足而失真、偏离训练分布 → 对 PPO 不公平。
_FEATURE_WARMUP_DAYS = 120


class PPOExposurePredictor:
    """加载 stage1 模型，把 (日期, 股票池, 账户状态) → 总暴露档位 ∈ {0,.25,.5,.75,1}。"""

    def __init__(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        model_path: Optional[str] = None,
        device: str = "cpu",
    ):
        """
        参数：
          symbols:    候选股票池（与回测一致；用于加载自持行情 + 市场特征横截面聚合）
          start_date: 回测起始日 "YYYY-MM-DD"（内部会再回扩 _FEATURE_WARMUP_DAYS 保证窗口完整）
          end_date:   回测结束日 "YYYY-MM-DD"
          model_path: stage1 模型路径（默认 models/ppo_stage1）
          device:     推理设备（默认 cpu；回测逐日推理，cpu 足够且省显存）
        """
        feature_start = (
            pd.Timestamp(start_date) - pd.Timedelta(days=_FEATURE_WARMUP_DAYS)
        ).strftime("%Y-%m-%d")
        # 自持行情：额外回扩预热期，保证每个回测决策日都有完整 60 日回看窗口。
        # 走缓存（宽文件覆盖此区间），不触发网络请求，离线可复现。
        self.market_data = load_market_data(symbols, feature_start, end_date, verbose=False)

        path = Path(model_path) if model_path else _MODEL_DIR / "ppo_stage1"
        # 不传 env：predict 只用 policy + 存盘的 observation_space；
        # 为推理而重建 PPOEnv 反而会被其 episode 长度校验挡住（回测区间不够长）。
        self.model = PPO.load(str(path), device=device)

    def predict_exposure(self, date, symbols: List[str], account: Dict) -> float:
        """
        给定决策日、聚合股票池、账户状态，返回 PPO 选择的总暴露档位。

        参数：
          date:    当前决策日（须在自持行情 index 中；缺失的市场特征按 features 内部规则跳过）
          symbols: 市场特征横截面聚合的股票（用候选池全池，与 stage1 评估口径一致）
          account: {"current_exposure": float, "drawdown": float, "recent_trades": int}

        返回：暴露档位 float ∈ {0.0, 0.25, 0.50, 0.75, 1.00}。
        """
        numeric = build_numeric_obs(self.market_data, date, symbols, account)
        # 阶段一主干只吃 numeric；llm 槽位填 0 占位以保持两阶段 obs 结构统一。
        obs = {"numeric": numeric.astype(np.float32), "llm": zeros_llm()}
        action, _ = self.model.predict(obs, deterministic=True)
        return float(EXPOSURE_LEVELS[int(action)])
