"""
阶段二 ResidualLLMPolicy 正确性测试

覆盖编译期与阶段一冒烟查不出的阶段二专属契约：
  1. zero-init：新建的旁路输出层严格为 0 → 阶段二起点 == 阶段一策略
  2. 权重迁移：主干从阶段一载入后，同输入下 value/logits 与阶段一一致（旁路未扰动）
  3. 冻结：freeze_backbone 后仅 adapter 参数 requires_grad，主干全 False
  4. 梯度流：backward 后旁路【输出层】能拿到非零梯度（zero-init 不会导致输出层学不动）
     —— 这是残差 adapter 能训练的前提，也是本轮排查 W2=0 的关键判据

运行：conda run --no-capture-output -n trader python tests/test_ppo_stage2.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import torch
import torch.nn as nn
from stable_baselines3 import PPO

from agents.stock_selector import DEFAULT_CANDIDATE_POOL
from core.ppo.data import load_market_data, build_llm_provider
from core.ppo.trainer import build_env
from core.ppo.policy import ResidualLLMPolicy, load_stage1_backbone
from core.ppo.features import LLM_DIM

_START, _END = "2025-08-11", "2026-08-09"
_ENV_KW = dict(subpool_size=8, episode_min_days=120, episode_max_days=200)
_PK = dict(net_arch=[64, 64], activation_fn=nn.Tanh, llm_dim=LLM_DIM, adapter_hidden=16)


def _load_data():
    return load_market_data(DEFAULT_CANDIDATE_POOL, _START, _END, verbose=False)


def _make_stage2(md, provider):
    env = build_env(md, DEFAULT_CANDIDATE_POOL, provider, 0, **_ENV_KW)
    model = PPO(ResidualLLMPolicy, env, policy_kwargs=_PK, learning_rate=1e-4,
                n_steps=64, batch_size=32, device="cpu", seed=0, verbose=0)
    return model, env


def test_zero_init_and_migration_and_grad():
    md = _load_data()
    provider = build_llm_provider(DEFAULT_CANDIDATE_POOL)

    # 阶段一模型（若已训练存盘则加载，否则现场建一个未训的做结构对照）
    s1_path = Path(_PROJECT_ROOT) / "models" / "ppo_stage1.zip"
    s1_env = build_env(md, DEFAULT_CANDIDATE_POOL, None, 0, **_ENV_KW)
    if s1_path.exists():
        s1 = PPO.load(str(s1_path), env=s1_env, device="cpu")
    else:
        from core.ppo.policy import make_stage1_policy_kwargs
        s1 = PPO("MultiInputPolicy", s1_env, policy_kwargs=make_stage1_policy_kwargs(),
                 device="cpu", seed=0, verbose=0)

    model, env = _make_stage2(md, provider)

    # --- 迁移前：旁路输出层必须严格 zero-init ---
    for name in ("actor_adapter.2.weight", "actor_adapter.2.bias",
                 "critic_adapter.2.weight", "critic_adapter.2.bias"):
        p = dict(model.policy.named_parameters())[name]
        assert torch.all(p == 0), f"{name} 未 zero-init"
    print("  [OK] 旁路输出层严格 zero-init（阶段二起点==阶段一）")

    # --- 迁移 + 冻结 ---
    missing, unexpected = load_stage1_backbone(model.policy, s1.policy)
    assert not unexpected, f"迁移出现 unexpected keys：{unexpected}"
    assert all("adapter" in k for k in missing), f"主干有未命中权重：{missing}"
    model.policy.freeze_backbone()

    trainable = [n for n, p in model.policy.named_parameters() if p.requires_grad]
    frozen = [n for n, p in model.policy.named_parameters() if not p.requires_grad]
    assert all("adapter" in n for n in trainable), f"非旁路参数竟可训：{trainable}"
    assert not any("adapter" in n for n in frozen), f"旁路参数竟被冻结：{frozen}"
    print(f"  [OK] 冻结正确：{len(trainable)} 个旁路张量可训，{len(frozen)} 个主干张量冻结")

    # --- 迁移后 value 与阶段一一致（旁路 zero → 不扰动）---
    obs, _ = env.reset(seed=5)
    ot = {k: torch.as_tensor(v).float().unsqueeze(0) for k, v in obs.items()}
    with torch.no_grad():
        v2 = model.policy.predict_values(ot).item()
        v1 = s1.policy.predict_values({"numeric": ot["numeric"]}).item()
    assert abs(v2 - v1) < 1e-5, f"迁移后 value 不一致：stage2={v2} stage1={v1}"
    print(f"  [OK] 迁移后 value 与阶段一一致（{v1:.6f} == {v2:.6f}），旁路 zero 未扰动")

    # --- 关键：梯度必须流到旁路【输出层】---
    logits, values = model.policy._fuse(ot)
    loss = logits.sum() + values.sum()
    model.policy.zero_grad()
    loss.backward()
    grads = {n: (None if p.grad is None else float(p.grad.abs().sum()))
             for n, p in model.policy.named_parameters() if "adapter" in n}
    for out_layer in ("actor_adapter.2.weight", "critic_adapter.2.weight"):
        g = grads[out_layer]
        assert g is not None and g > 0, \
            f"{out_layer} 梯度为 {g}——输出层学不动，残差 adapter 失效！"
    print(f"  [OK] 旁路输出层梯度非零：actor.2={grads['actor_adapter.2.weight']:.3e}, "
          f"critic.2={grads['critic_adapter.2.weight']:.3e}")

    # 隐藏层在 W2=0 时梯度应为 0（链式法则：dL/dW1 = W2^T·... = 0），这是 zero-init 的预期行为
    assert grads["actor_adapter.0.weight"] == 0.0, \
        "W2=0 时隐藏层梯度应为 0（一旦 W2 离开零，下一步隐藏层才会激活）"
    print("  [OK] 隐藏层初始梯度为 0（符合 zero-init 残差的链式法则；W2 先动、随后带动 W1）")


def test_predict_includes_adapter():
    """
    回归：推理链 predict 必须与 forward 一致地【含旁路】。

    曾经的 bug（消融 Δ≡ 0 排查所得）：SB3 predict → _predict → get_distribution().get_actions()
    【不走 forward】；父类 get_distribution 用 extract_features→_get_action_dist_from_latent，
    只算主干 action_net、丢掉 llm 旁路 → 部署/回测/消融时 LLM 完全不参与决策，与训练
    （forward 含旁路）不一致。修法：ResidualLLMPolicy 覆盖 get_distribution 走 _fuse。

    本测用【恒定强推 top 档】的旁路把差异放大到必然可观测：
      W2=0 → 旁路输出≡ b2（与 llm 无关），置 b2[top]=10 → logits argmax 必为 top。
      若推理链丢旁路，predict 会退回主干 argmax（非 top），断言即失败。
    """
    md = _load_data()
    provider = build_llm_provider(DEFAULT_CANDIDATE_POOL)
    model, env = _make_stage2(md, provider)

    top = model.policy.action_space.n - 1
    with torch.no_grad():
        model.policy.actor_adapter[-1].weight.zero_()
        model.policy.actor_adapter[-1].bias.zero_()
        model.policy.actor_adapter[-1].bias[top] = 10.0

    obs, _ = env.reset(seed=7)
    ot = {k: torch.as_tensor(v).float().unsqueeze(0) for k, v in obs.items()}
    with torch.no_grad():
        logits, _ = model.policy._fuse(ot)
    assert int(logits.argmax()) == top, "构造失败：_fuse argmax 应为 top 档"

    pred, _ = model.policy.predict(obs, deterministic=True)
    pred = int(np.asarray(pred).reshape(-1)[0])
    assert pred == top, (
        f"predict 给出 {pred}（应为 {top}）——推理链丢了旁路！"
        f"get_distribution 未走 _fuse，部署/回测/消融时 LLM 形同虚设")
    print(f"  [OK] predict 含旁路：强推 top 档时 predict 与 _fuse 一致（action={top}）")


if __name__ == "__main__":
    print("=" * 60)
    print("  阶段二 ResidualLLMPolicy 正确性测试")
    print("=" * 60)
    test_zero_init_and_migration_and_grad()
    test_predict_includes_adapter()
    print("=" * 60)
    print("  全部通过 ✓")
    print("=" * 60)
