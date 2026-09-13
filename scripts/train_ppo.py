#!/usr/bin/env python3
"""
PPO 两阶段训练 CLI

用法：
  # 阶段一：纯 13 维数值主干预训练（通用仓位择时）
  python scripts/train_ppo.py --stage 1 --timesteps 50000

  # 阶段二：冻结主干 + 5 维 LLM 旁路微调（需先有阶段一模型）
  python scripts/train_ppo.py --stage 2 --timesteps2 20000

  # 两阶段连跑（阶段一模型内存直传阶段二，不落盘重载）
  python scripts/train_ppo.py --stage both

  # 冒烟：小规模快速验证管线（不追求收敛）
  python scripts/train_ppo.py --stage both --timesteps 512 --timesteps2 256
"""

import sys
from pathlib import Path

# 让直接运行（python scripts/xxx.py）也能找到 core 模块
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse

from core import config
from agents.stock_selector import DEFAULT_CANDIDATE_POOL
from core.ppo.data import load_market_data, build_llm_provider
from core.ppo.trainer import train_stage1, train_stage2

# 默认模型名（落到 config.PROJECT_ROOT/models/，SB3 自动加 .zip）
_STAGE1_NAME = "ppo_stage1"
_STAGE2_NAME = "ppo_stage2"


def parse_args():
    p = argparse.ArgumentParser(description="PPO 两阶段训练（阶段一数值主干 / 阶段二 LLM 旁路）")
    p.add_argument("--stage", choices=["1", "2", "both"], default="1",
                   help="训练哪个阶段（默认 1）")
    p.add_argument("--pool", nargs="+", default=None,
                   help=f"候选股票池（默认 10 只）：{DEFAULT_CANDIDATE_POOL}")
    # 阶段一（数值主干）长历史区间，与阶段二不相交
    p.add_argument("--start1", default=config.PPO_STAGE1_START_DATE, help="阶段一起始日 YYYY-MM-DD（默认 2015-01-01）")
    p.add_argument("--end1", default=config.PPO_STAGE1_END_DATE, help="阶段一结束日 YYYY-MM-DD（默认 2022-12-31）")
    # 阶段二（LLM 微调）近一年区间
    p.add_argument("--start", default=config.DEFAULT_START_DATE, help="阶段二起始日 YYYY-MM-DD")
    p.add_argument("--end", default=config.DEFAULT_END_DATE, help="阶段二结束日 YYYY-MM-DD")
    p.add_argument("--timesteps", type=int, default=50_000, help="阶段一总步数")
    p.add_argument("--timesteps2", type=int, default=20_000, help="阶段二总步数")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument("--device", default="auto", help="训练设备（auto/cpu/cuda）")
    # env / 数据增广参数
    p.add_argument("--subpool-size", type=int, default=config.TOP_K,
                   help="每 episode 随机抽取的子池大小（默认 TOP_K，对齐真实回测选股数）")
    p.add_argument("--episode-min", type=int, default=120, help="episode 最短天数")
    p.add_argument("--episode-max", type=int, default=480, help="episode 最长天数")
    # 阶段二加载的阶段一模型路径
    p.add_argument("--stage1-model", default=None,
                   help=f"阶段二加载的阶段一模型路径（默认 models/{_STAGE1_NAME}）")
    args = p.parse_args()

    if args.pool is None:
        args.pool = DEFAULT_CANDIDATE_POOL
    return args


def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  PPO 两阶段训练")
    print("=" * 60)
    print(f"  阶段：{args.stage}  股票池：{len(args.pool)} 只")
    print(f"  阶段一区间：{args.start1} ~ {args.end1}   阶段二区间：{args.start} ~ {args.end}")
    print(f"  seed={args.seed}  device={args.device}  "
          f"subpool={args.subpool_size}  episode=[{args.episode_min},{args.episode_max}]")

    env_kwargs = dict(
        subpool_size=args.subpool_size,
        episode_min_days=args.episode_min,
        episode_max_days=args.episode_max,
    )

    stage1_model = None

    # 2. 阶段一：长历史 2015-2022（跨周期，防单一 regime 过拟合）
    if args.stage in ("1", "both"):
        print("\n  [1] 阶段一加载行情 + 计算指标（2015-2022 长历史）...")
        market_data_s1 = load_market_data(args.pool, args.start1, args.end1)
        print("\n  [2] 阶段一：纯数值主干预训练")
        stage1_model = train_stage1(
            market_data=market_data_s1,
            symbol_pool=args.pool,
            total_timesteps=args.timesteps,
            seed=args.seed,
            save_path=_STAGE1_NAME,
            device=args.device,
            **env_kwargs,
        )

    # 3. 阶段二：近一年 2025-2026（与阶段一区间不相交）
    if args.stage in ("2", "both"):
        print("\n  [3] 阶段二加载行情 + 计算指标（2025-2026）...")
        market_data_s2 = load_market_data(args.pool, args.start, args.end)
        print("\n  [4] 阶段二：冻结主干 + LLM 旁路微调")
        # 阶段一来源：both 用内存模型；单独 stage2 从盘加载
        stage1_src = stage1_model if stage1_model is not None else \
            (args.stage1_model or (config.PROJECT_ROOT / "models" / _STAGE1_NAME))
        if stage1_model is None:
            print(f"  从盘加载阶段一模型：{stage1_src}")

        # 注入 PIT LLM 信号 provider（按当前 model + prompt_version 过滤）
        llm_provider = build_llm_provider(args.pool)

        train_stage2(
            market_data=market_data_s2,
            llm_provider=llm_provider,
            stage1=stage1_src,
            symbol_pool=args.pool,
            total_timesteps=args.timesteps2,
            seed=args.seed,
            save_path=_STAGE2_NAME,
            device=args.device,
            **env_kwargs,
        )

    print("\n" + "=" * 60)
    print("  训练完成！模型在 models/ 目录")
    print("=" * 60)


if __name__ == "__main__":
    main()
