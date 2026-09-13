"""
LLM 边际贡献量化（消融 / ablation）

阶段二 ResidualLLMPolicy 的旁路输出，本质就是「LLM 语义信号对动作与价值的修正量」。
把旁路整体关掉（policy.use_adapter=False），策略退化回【冻结的纯数值主干】——即阶段一。
于是同一模型、同一批 episode 下：

    ΔPerf = Perf(旁路开) − Perf(旁路关) = LLM 的边际贡献（「消融红利」）

为什么这样归因是干净的：
  1. 用同一个 stage2 模型切换 use_adapter，主干权重逐字节相同 → 差值只可能来自旁路；
  2. 两趟 rollout 用同一 seed 逐个 reset → 子池/起点/长度完全一致，numeric 与日收益相同；
  3. deterministic=True（贪心）→ 无采样噪声，delta 可复现。

指标口径与 recorder 的评估对齐（收益/夏普/回撤），便于和公式版基线横向比较。

运行（需先有 models/ppo_stage2.zip）：
    python -m core.ppo.ablation --n-episodes 20
"""

import sys
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
import math

import numpy as np
from stable_baselines3 import PPO

from core import config
from core.ppo.data import load_market_data, build_llm_provider
from core.ppo.trainer import build_env

# 年化周期数（夏普年化用）：env 周频改造后每 step = 一周，故取 52 周/年
_ANNUAL_PERIODS = 52

# 模型默认目录（与 trainer._MODEL_DIR 一致）
_MODEL_DIR = config.PROJECT_ROOT / "models"


def _episode_metrics(net_rets: List[float], exposures: List[float]) -> Dict[str, float]:
    """
    单 episode 的交易绩效指标（全部由「净收益序列」推导，与 recorder 口径一致）。

    net_ret 已扣换手成本（见 env.step），故累计净值即「费后」表现。
    """
    r = np.asarray(net_rets, dtype=np.float64)
    if r.size == 0:
        return {"total_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0,
                "mean_exposure": 0.0, "n_weeks": 0}

    equity = np.cumprod(1.0 + r)
    total_return = float(equity[-1] - 1.0)

    sd = float(r.std())
    sharpe = float(r.mean() / sd * math.sqrt(_ANNUAL_PERIODS)) if sd > 1e-12 else 0.0

    peak = np.maximum.accumulate(equity)
    max_drawdown = float(np.max((peak - equity) / peak)) if peak[-1] > 0 else 0.0

    mean_exposure = float(np.mean(exposures)) if exposures else 0.0
    return {"total_return": total_return, "sharpe": sharpe,
            "max_drawdown": max_drawdown, "mean_exposure": mean_exposure,
            "n_weeks": int(r.size)}


def _aggregate(per_ep: List[Dict[str, float]]) -> Dict[str, float]:
    """跨 episode 求均值（n_days 取平均，仅作规模参考）。"""
    keys = per_ep[0].keys()
    return {k: float(np.mean([ep[k] for ep in per_ep])) for k in keys}


def evaluate_policy(
    model: PPO,
    env,
    n_episodes: int = 20,
    seed: int = 0,
    deterministic: bool = True,
    use_adapter: bool = True,
) -> Dict[str, float]:
    """
    在 env 上跑 n_episodes 个确定性 episode，返回聚合绩效。

    use_adapter 控制旁路开关：True=带 LLM 修正，False=纯数值主干（消融基线）。
    每个 episode 用 seed+ep 复位 → 换用不同子池/窗口，均值更稳健。
    """
    # 临时切换旁路开关，退出时恢复（不污染调用方对模型的其他使用）
    prev = getattr(model.policy, "use_adapter", True)
    model.policy.use_adapter = use_adapter
    try:
        per_ep: List[Dict[str, float]] = []
        for ep in range(n_episodes):
            obs, _ = env.reset(seed=seed + ep)
            rets: List[float] = []
            exposures: List[float] = []
            terminated = truncated = False
            while not (terminated or truncated):
                action, _ = model.predict(obs, deterministic=deterministic)
                obs, _reward, terminated, truncated, info = env.step(action)
                rets.append(info["net_ret"])
                exposures.append(info["exposure"])
            per_ep.append(_episode_metrics(rets, exposures))
        return _aggregate(per_ep)
    finally:
        model.policy.use_adapter = prev


def ablate_llm(model: PPO, env, n_episodes: int = 20, seed: int = 0) -> Dict[str, Dict[str, float]]:
    """
    LLM 边际贡献量化：同一 stage2 模型，旁路开 vs 关，同批 episode 对比。

    返回 {"with_llm": {...}, "without_llm": {...}, "delta": {...}}，
    delta[k] = with_llm[k] − without_llm[k]（收益/夏普为正=LLM 有增益；回撤为负=LLM 降风险）。
    """
    with_llm = evaluate_policy(model, env, n_episodes, seed, use_adapter=True)
    without_llm = evaluate_policy(model, env, n_episodes, seed, use_adapter=False)
    delta = {k: with_llm[k] - without_llm[k] for k in with_llm}
    return {"with_llm": with_llm, "without_llm": without_llm, "delta": delta}


def _print_table(result: Dict[str, Dict[str, float]]) -> None:
    """打印消融对照表（收益/夏普/回撤/暴露）。"""
    rows = [
        ("累计收益", "total_return", "{:+.2%}"),
        ("年化夏普", "sharpe", "{:+.3f}"),
        ("最大回撤", "max_drawdown", "{:.2%}"),
        ("平均暴露", "mean_exposure", "{:.2%}"),
    ]
    w, wo, d = result["with_llm"], result["without_llm"], result["delta"]
    print(f"  {'指标':<10}{'纯数值主干':>14}{'+LLM旁路':>14}{'Δ(LLM边际)':>16}")
    print("  " + "-" * 54)
    for label, key, fmt in rows:
        print(f"  {label:<10}{fmt.format(wo[key]):>14}{fmt.format(w[key]):>14}"
              f"{fmt.format(d[key]):>16}")
    print(f"  （episode 数={int(round(w['n_weeks']))} 周/段均值，样本口径一致）")


def parse_args():
    from agents.stock_selector import DEFAULT_CANDIDATE_POOL
    p = argparse.ArgumentParser(description="LLM 边际贡献量化（阶段二旁路消融）")
    p.add_argument("--stage2-model", default=str(_MODEL_DIR / "ppo_stage2"),
                   help="阶段二模型路径（默认 models/ppo_stage2）")
    p.add_argument("--pool", nargs="+", default=DEFAULT_CANDIDATE_POOL, help="候选股票池")
    p.add_argument("--start", default=config.DEFAULT_START_DATE, help="起始日 YYYY-MM-DD")
    p.add_argument("--end", default=config.DEFAULT_END_DATE, help="结束日 YYYY-MM-DD")
    p.add_argument("--n-episodes", type=int, default=20, help="评估 episode 数（换不同子池/窗口）")
    p.add_argument("--seed", type=int, default=0, help="评估起始 seed（两趟共用，保证同批 episode）")
    p.add_argument("--device", default="cpu", help="加载设备（消融只需推理，默认 cpu）")
    # env 参数：默认用全池 + 尽量长的 episode，逼近「近全周期」对照
    p.add_argument("--subpool-size", type=int, default=None, help="子池大小（默认=全池）")
    p.add_argument("--episode-min", type=int, default=120, help="episode 最短天数")
    p.add_argument("--episode-max", type=int, default=180, help="episode 最长天数")
    return p.parse_args()


def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  LLM 边际贡献量化（阶段二旁路消融）")
    print("=" * 60)
    print(f"  模型：{args.stage2_model}  区间：{args.start} ~ {args.end}  "
          f"episodes={args.n_episodes}")

    print("\n  [1] 加载行情 + LLM 信号...")
    market_data = load_market_data(args.pool, args.start, args.end, verbose=False)
    provider = build_llm_provider(args.pool)

    subpool = args.subpool_size or len(args.pool)
    env = build_env(
        market_data, args.pool, provider, args.seed,
        subpool_size=subpool,
        episode_min_days=args.episode_min,
        episode_max_days=args.episode_max,
    )

    print("  [2] 加载阶段二模型并跑双趟对照（旁路开/关，同批 episode）...")
    model = PPO.load(args.stage2_model, env=env, device=args.device)
    result = ablate_llm(model, env, n_episodes=args.n_episodes, seed=args.seed)

    print("\n  [3] 消融结果：")
    _print_table(result)

    d = result["delta"]
    print("\n  结论：", end="")
    if d["total_return"] > 0 and d["sharpe"] > 0:
        print(f"LLM 旁路带来正边际贡献（收益 {d['total_return']:+.2%}，夏普 {d['sharpe']:+.3f}）")
    elif d["total_return"] <= 0 and d["sharpe"] <= 0:
        print(f"LLM 旁路未见增益（收益 {d['total_return']:+.2%}，夏普 {d['sharpe']:+.3f}）"
              f"——检查信号质量/训练步数")
    else:
        print(f"LLM 旁路影响不一致（收益 {d['total_return']:+.2%}，夏普 {d['sharpe']:+.3f}）")
    print("=" * 60)


if __name__ == "__main__":
    main()
