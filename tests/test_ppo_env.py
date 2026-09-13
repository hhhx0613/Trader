"""
PPOEnv 运行冒烟测试（阶段一模式，llm_provider=None）

用真实缓存行情验证 env 的运行时正确性，重点覆盖编译期查不出的问题：
  1. obs 结构/dtype/空间包含性（Dict{numeric:13, llm:5}，float32）
  2. numeric 特征真的从指标列算出（非全 0）——捕捉 features.py 里 .get(默认值) 静默退化
  3. 阶段一 llm 分支恒为 0
  4. 整段 episode 奖励全程有限（差分夏普递推不炸 NaN/Inf）
  5. episode 在预期长度处 truncated、terminated 恒 False

运行：conda run -n trader python tests/test_ppo_env.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

from agents.stock_selector import DEFAULT_CANDIDATE_POOL
from core.ppo.data import load_market_data
from core.ppo.env import PPOEnv, EXPOSURE_LEVELS
from core.ppo.features import NUMERIC_DIM, LLM_DIM, _LOOKBACK

# 缓存覆盖的精确区间（*_2025-06-12_2026-08-06.csv，全 10 只均有），离线可复现
_START, _END = "2025-06-12", "2026-08-06"


def _build_env(seed: int = 42) -> PPOEnv:
    market_data = load_market_data(DEFAULT_CANDIDATE_POOL, _START, _END, verbose=False)
    assert len(market_data) == len(DEFAULT_CANDIDATE_POOL), \
        f"应加载 {len(DEFAULT_CANDIDATE_POOL)} 只，实际 {len(market_data)} 只（缓存缺失？）"
    return PPOEnv(
        market_data=market_data,
        symbol_pool=DEFAULT_CANDIDATE_POOL,
        llm_provider=None,          # 阶段一
        subpool_size=5,
        episode_min_days=120,
        episode_max_days=200,
        seed=seed,
    )


def test_obs_structure():
    env = _build_env()
    obs, info = env.reset(seed=0)

    assert isinstance(obs, dict) and set(obs.keys()) == {"numeric", "llm"}, obs.keys()
    assert obs["numeric"].shape == (NUMERIC_DIM,), obs["numeric"].shape
    assert obs["llm"].shape == (LLM_DIM,), obs["llm"].shape
    assert obs["numeric"].dtype == np.float32 and obs["llm"].dtype == np.float32
    assert env.observation_space.contains(obs), "obs 不在 observation_space 内"

    # 阶段一：llm 分支必须全 0
    assert np.all(obs["llm"] == 0.0), f"阶段一 llm 应恒 0，实际 {obs['llm']}"

    # numeric 非全 0：证明特征真从指标列算出（否则 .get 默认值会让它退化）
    assert np.any(obs["numeric"] != 0.0), \
        f"numeric 全 0，特征可能静默退化为默认值：{obs['numeric']}"
    # 账户 3 维初始应为 [exposure=0, drawdown=0, freq=0]
    assert np.allclose(obs["numeric"][-3:], [0.0, 0.0, 0.0]), obs["numeric"][-3:]
    print(f"  [OK] obs 结构/空间/阶段一 llm=0；numeric={np.round(obs['numeric'], 3).tolist()}")


def test_full_episode_rewards_finite():
    env = _build_env()
    obs, _ = env.reset(seed=7)
    total, steps, actions_seen = 0.0, 0, set()
    terminated = truncated = False

    while not (terminated or truncated):
        action = env.action_space.sample()
        actions_seen.add(int(action))
        obs, reward, terminated, truncated, info = env.step(action)

        assert np.isfinite(reward), f"第 {steps} 步奖励非有限：{reward}"
        assert env.observation_space.contains(obs), f"第 {steps} 步 obs 越界"
        assert np.all(np.isfinite(obs["numeric"])), f"第 {steps} 步 numeric 含 NaN/Inf"
        assert 0.0 <= info["exposure"] <= 1.0
        total += reward
        steps += 1

    assert truncated and not terminated, "应时间截断（truncated=True, terminated=False）"
    # 周频改造后：每 step = 一个 ISO 周，steps 是周数而非天数
    assert env._min_ep_weeks <= steps <= env._max_ep_weeks, \
        f"episode 周数 {steps} 超出 [{env._min_ep_weeks}, {env._max_ep_weeks}]"
    print(f"  [OK] 整段 {steps} 周奖励全程有限，累计={total:.3f}，"
          f"末值 equity={info['equity']:.4f}，覆盖动作档={sorted(actions_seen)}")


def test_deterministic_reset():
    """同 seed reset 应得到相同子池与起点（可复现性）。"""
    env = _build_env()
    o1, _ = env.reset(seed=123)
    o2, _ = env.reset(seed=123)
    assert np.array_equal(o1["numeric"], o2["numeric"]), "同 seed reset 不可复现"
    print("  [OK] 同 seed reset 可复现")


def test_exposure_levels():
    assert EXPOSURE_LEVELS.tolist() == [0.0, 0.25, 0.50, 0.75, 1.00], EXPOSURE_LEVELS
    assert len(EXPOSURE_LEVELS) == 5
    print(f"  [OK] 暴露档位={EXPOSURE_LEVELS.tolist()}")


if __name__ == "__main__":
    print("=" * 60)
    print("  PPOEnv 冒烟测试（阶段一，真实缓存行情）")
    print("=" * 60)
    test_exposure_levels()
    test_obs_structure()
    test_full_episode_rewards_finite()
    test_deterministic_reset()
    print("=" * 60)
    print("  全部通过 ✓")
    print("=" * 60)
