"""
PPO 两阶段训练编排（trainer）

阶段一（train_stage1）：纯 13 维数值主干，标准 PPO 预训练通用仓位择时。
阶段二（train_stage2）：冻结主干 + 5 维 LLM 旁路微调（ResidualLLMPolicy）。

职责边界（SOLID）：
  - 本模块只做「训练编排」：建 env、配 PPO、迁移权重、冻结、learn、存盘。
  - 数据加载在 data.py，特征在 features.py，网络在 policy.py，环境在 env.py。
  - llm_provider 由调用方注入（依赖倒置）——trainer 不碰 LLM 缓存 DB 细节。

关键正确性约束（阶段二）：
  1. 两阶段主干架构必须完全一致（net_arch/activation/extractor），否则权重迁移错位。
  2. 迁移后校验：unexpected_keys 必须为空（空=主干结构吻合）；missing 应全是 adapter.*。
  3. 先迁移、再冻结、后 learn：freeze_backbone 会重建 optimizer 只喂旁路参数。
"""

from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import pandas as pd
import torch.nn as nn
from stable_baselines3 import PPO

from core import config
from core.ppo.env import PPOEnv
from core.ppo.features import LLM_DIM
from core.ppo.policy import (
    make_stage1_policy_kwargs, ResidualLLMPolicy, load_stage1_backbone,
)

# 主干架构（两阶段共用，改动会使已训权重无法迁移）
_BACKBONE_NET_ARCH = [64, 64]
_BACKBONE_ACTIVATION = nn.Tanh
_ADAPTER_HIDDEN = 16

# 模型默认存盘目录
_MODEL_DIR = config.PROJECT_ROOT / "models"


def build_env(
    market_data: Dict[str, pd.DataFrame],
    symbol_pool: Optional[List[str]] = None,
    llm_provider: Optional[Callable] = None,
    seed: Optional[int] = None,
    **env_kwargs,
) -> PPOEnv:
    """构造 PPOEnv（阶段一 llm_provider=None，阶段二注入真实 provider）。"""
    return PPOEnv(
        market_data=market_data,
        symbol_pool=symbol_pool,
        llm_provider=llm_provider,
        seed=seed,
        **env_kwargs,
    )


def train_stage1(
    market_data: Dict[str, pd.DataFrame],
    symbol_pool: Optional[List[str]] = None,
    total_timesteps: int = 50_000,
    seed: int = 42,
    save_path: Optional[Union[str, Path]] = None,
    learning_rate: float = 3e-4,
    n_steps: int = 256,
    batch_size: int = 64,
    n_epochs: int = 10,
    gamma: float = 0.99,
    gae_lambda: float = 0.95,
    ent_coef: float = 0.01,
    clip_range: float = 0.2,
    device: str = "auto",
    verbose: int = 1,
    **env_kwargs,
) -> PPO:
    """
    阶段一：纯数值主干预训练。

    为什么用 "MultiInputPolicy" + NumericExtractor 而非 ResidualLLMPolicy：
      阶段一 llm 分支恒 0，无需旁路；标准 policy 最简单、最稳（KISS）。
      obs 是 Dict，SB3 强制要求 MultiInputPolicy（"MlpPolicy" 会被拒）；再用 policy_kwargs
      把默认的 CombinedExtractor 换成 NumericExtractor，保证主干只吃 numeric(13)，
      与阶段二冻结前提一致。

    返回训练好的 PPO 模型（若 save_path 非空则同时存盘）。
    """
    env = build_env(market_data, symbol_pool, llm_provider=None, seed=seed, **env_kwargs)
    policy_kwargs = make_stage1_policy_kwargs(net_arch=_BACKBONE_NET_ARCH)

    model = PPO(
        "MultiInputPolicy", env,
        policy_kwargs=policy_kwargs,
        learning_rate=learning_rate,
        # PPO 学习循环超参（on-policy：攒一批→复盘→更新→丢弃→再攒）
        n_steps=n_steps, batch_size=batch_size, n_epochs=n_epochs,  # 每批采集步数 / minibatch / 同批复用遍数
        gamma=gamma, gae_lambda=gae_lambda,                         # 折扣因子 / GAE 的 λ（优势 A 看多远）
        ent_coef=ent_coef, clip_range=clip_range,                   # 熵奖励(促探索) / clip(限单次更新幅度)
        seed=seed, device=device, verbose=verbose,
    )
    # learn() 内部才是真正的「学习」，循环：①用当前策略采集 n_steps 步 →
    # ②GAE 为每步算优势 A_t=实际−V(s_t) → ③n_epochs 遍 minibatch 更新(A>0 提该动作概率、
    # A<0 降) → ④丢弃这批、用新策略重采。本项目 env/policy/data 都是喂给它的「插件」，
    # 学习算法本体在 SB3 内部（不在本仓库）。
    model.learn(total_timesteps=total_timesteps)

    if save_path is not None:
        _save(model, save_path)
    return model


def train_stage2(
    market_data: Dict[str, pd.DataFrame],
    llm_provider: Callable,
    stage1: Union[PPO, str, Path],
    symbol_pool: Optional[List[str]] = None,
    total_timesteps: int = 20_000,
    seed: int = 42,
    save_path: Optional[Union[str, Path]] = None,
    adapter_lr: float = 1e-4,
    n_steps: int = 256,
    batch_size: int = 64,
    n_epochs: int = 10,
    gamma: float = 0.99,
    gae_lambda: float = 0.95,
    ent_coef: float = 0.01,
    clip_range: float = 0.2,
    device: str = "auto",
    verbose: int = 1,
    **env_kwargs,
) -> PPO:
    """
    阶段二：冻结主干 + LLM 旁路微调。

    参数：
      llm_provider: callable(date, symbols) -> llm_obs(5)，由调用方注入（见 data.py 面板）
      stage1:       阶段一模型（PPO 实例）或其存盘路径

    流程：建 env(带 llm) → 建 ResidualLLMPolicy → 迁移主干 → 校验 → 冻结 → learn。
    """
    stage1_model = _load_stage1(stage1, market_data, symbol_pool, seed, device, env_kwargs)

    env = build_env(market_data, symbol_pool, llm_provider=llm_provider, seed=seed, **env_kwargs)
    # 阶段二 policy_kwargs：主干架构必须与阶段一逐字一致，否则迁移错位
    policy_kwargs = {
        "net_arch": _BACKBONE_NET_ARCH,
        "activation_fn": _BACKBONE_ACTIVATION,
        "llm_dim": LLM_DIM,
        "adapter_hidden": _ADAPTER_HIDDEN,
    }

    model = PPO(
        ResidualLLMPolicy, env,
        policy_kwargs=policy_kwargs,
        learning_rate=adapter_lr,
        n_steps=n_steps, batch_size=batch_size, n_epochs=n_epochs,
        gamma=gamma, gae_lambda=gae_lambda,
        ent_coef=ent_coef, clip_range=clip_range,
        seed=seed, device=device, verbose=verbose,
    )

    # 迁移主干权重（旁路保持 zero-init）并校验
    missing, unexpected = load_stage1_backbone(model.policy, stage1_model.policy)
    if unexpected:
        raise RuntimeError(
            f"阶段一→二权重迁移出现 unexpected keys（主干结构不匹配）：{unexpected}")
    non_adapter_missing = [k for k in missing if "adapter" not in k]
    if non_adapter_missing:
        raise RuntimeError(
            f"主干有未迁移的权重（应全部命中）：{non_adapter_missing}")
    if verbose:
        print(f"  [迁移] 主干权重已载入；{len(missing)} 个 adapter 参数保持 zero-init（预期内）")

    # 冻结主干（仅切 requires_grad；lr 由上面 PPO(learning_rate=adapter_lr) 统一控制）
    trainable = model.policy.freeze_backbone()
    if verbose:
        n_params = sum(p.numel() for p in trainable)
        print(f"  [冻结] 主干全冻结，仅 {len(trainable)} 个旁路张量可训（{n_params} 参数）")

    # 学习循环同 stage1，但主干已冻结 → learn() 只更新旁路(adapter)参数，学「LLM 该修正多少」
    model.learn(total_timesteps=total_timesteps)

    if save_path is not None:
        _save(model, save_path)
    return model


# ---------- 内部工具 ----------

def _load_stage1(stage1, market_data, symbol_pool, seed, device, env_kwargs) -> PPO:
    """stage1 可为 PPO 实例或路径；路径则从盘加载（需临时 env 供 SB3 重建）。"""
    if isinstance(stage1, PPO):
        return stage1
    tmp_env = build_env(market_data, symbol_pool, llm_provider=None, seed=seed, **env_kwargs)
    return PPO.load(str(stage1), env=tmp_env, device=device)


def _save(model: PPO, save_path: Union[str, Path]) -> Path:
    """存盘（SB3 自动追加 .zip）；相对名落到 models/ 目录。"""
    path = Path(save_path)
    if not path.is_absolute():
        path = _MODEL_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    model.save(path)
    print(f"  [存盘] 模型已保存到 {path}.zip")
    return path
