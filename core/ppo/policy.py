"""
PPO 策略网络（policy）

提供两阶段所需的策略：
  - NumericExtractor：两阶段共用，从 Dict obs 中只取 numeric(13) 喂主干
  - 阶段一：标准 ActorCriticPolicy + NumericExtractor（忽略 llm 分支，见 make_stage1_policy_kwargs）
  - 阶段二：ResidualLLMPolicy —— 冻结主干 + actor/critic 双旁路（residual adapter）

关键设计（见 Plan_v2.md 阶段 6B）：
  1. 主干永远只吃 numeric(13)，两阶段 features_dim 一致 → 主干权重可直接迁移。
  2. actor 与 critic 各挂一个旁路：critic 若看不到 LLM 特征，advantage A=Q−V 会系统性有偏。
  3. 旁路末层 zero-init → 阶段二起点严格等于阶段一策略，从零学「修正量」。
  4. 覆盖 forward/get_distribution/evaluate_actions/predict_values，全部走 _fuse：
     SB3 默认实现（extract_features→_get_action_dist_from_latent）会先把 Dict 压成 numeric、
     丢掉 llm 分支，导致旁路拿不到输入。尤其 predict→_predict→get_distribution 这条【推理链】
     不走 forward：若漏掉 get_distribution，会出现「训练用旁路、部署/回测/消融却丢旁路」，
     LLM 形同虚设（本轮排查消融 Δ≡ 0 即因此）。
"""

from typing import Callable, Dict, List, Optional, Tuple

import gymnasium as gym
import torch
import torch.nn as nn
from stable_baselines3.common.policies import ActorCriticPolicy
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor

from core.ppo.features import NUMERIC_DIM, LLM_DIM


# ==================== 特征提取器：只取 numeric 分支 ====================

class NumericExtractor(BaseFeaturesExtractor):
    """
    从 Dict{numeric, llm} 观测中只取 numeric(13) 喂给主干。

    为什么需要它：SB3 对 Dict obs 默认用 CombinedExtractor（会把 numeric+llm 拼在一起），
    那样主干就会吃到 llm，违背「主干永远只看 13 维数值」的冻结前提。
    本提取器保证阶段一/二主干输入分布严格一致，杜绝阶段二冻结主干遇到 OOD。
    """

    def __init__(self, observation_space: gym.Space):
        super().__init__(observation_space, features_dim=NUMERIC_DIM)

    def forward(self, observations: Dict[str, torch.Tensor]) -> torch.Tensor:
        return observations["numeric"]


# ==================== 阶段一：标准 PPO 的 policy_kwargs ====================

def make_stage1_policy_kwargs(net_arch: Optional[List[int]] = None) -> Dict:
    """
    阶段一预训练的 policy_kwargs：标准 ActorCriticPolicy，但用 NumericExtractor 只吃 numeric。

    env 的 obs 仍是 Dict{numeric, llm}（阶段一 llm 填 0），标准 policy 经 NumericExtractor
    自动忽略 llm 分支——这样阶段一/二 env 结构完全一致，只是 policy 类与 llm 内容不同。
    """
    return {
        "features_extractor_class": NumericExtractor,
        "net_arch": net_arch or [64, 64],
        "activation_fn": nn.Tanh,   # 对齐 Plan_v2「64×64 tanh 网络」
    }


# ==================== 阶段二：Residual LLM Adapter ====================

class ResidualLLMPolicy(ActorCriticPolicy):
    """
    冻结主干 + LLM 旁路的 residual adapter 策略（阶段二微调专用）。

    结构：
      logits = 主干action_net(latent_pi) + actor_adapter(llm5)
      value  = 主干value_net(latent_vf)  + critic_adapter(llm5)
    旁路末层 zero-init，故初始等价于阶段一策略；训练时仅旁路参数更新（见 freeze_backbone）。
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        lr_schedule: Callable[[float], float],
        net_arch: Optional[List[int]] = None,
        activation_fn: type = nn.Tanh,
        llm_dim: int = LLM_DIM,
        adapter_hidden: int = 16,
        **kwargs,
    ):
        self.llm_dim = llm_dim
        self.adapter_hidden = adapter_hidden
        # 消融开关：False 时旁路整体旁路，策略退化回「冻结的纯数值主干」（==阶段一）。
        # 不入 state_dict，仅推理期临时切换；默认 True 保持训练/预测行为不变。
        self.use_adapter = True
        # 强制主干用 NumericExtractor（只吃 numeric 13 维），与阶段一一致
        kwargs["features_extractor_class"] = NumericExtractor
        super().__init__(
            observation_space, action_space, lr_schedule,
            net_arch=net_arch or [64, 64],
            activation_fn=activation_fn,
            **kwargs,
        )
        # 旁路：actor（→动作 logits）+ critic（→value），二者缺一不可
        n_actions = action_space.n
        self.actor_adapter = nn.Sequential(
            nn.Linear(llm_dim, adapter_hidden), nn.ReLU(),
            nn.Linear(adapter_hidden, n_actions),
        )
        self.critic_adapter = nn.Sequential(
            nn.Linear(llm_dim, adapter_hidden), nn.ReLU(),
            nn.Linear(adapter_hidden, 1),
        )
        self._zero_init_adapters()
        # 关键：旁路在 super().__init__() 之后才创建，未被其内部构建的 optimizer 收录
        # （super 建 optimizer 时 self.parameters() 只有主干）。这里用【全部参数】重建：
        #   ① 旁路进优化器才会被训练（否则永远停在 zero-init）；
        #   ② 训练时与 PPO.load 重新构造时参数组大小一致（都含旁路），save/load 不再报
        #      "parameter group doesn't match"。
        # 冻结靠 requires_grad（见 freeze_backbone）：主干 grad=None 时 Adam 自动跳过，
        # 无需从优化器剔除，故参数组构成在两处保持一致。
        # 注意 lr_schedule 是 __init__ 的局部形参（SB3 不存为 self.lr_schedule），
        # 与父类 _build 内部构建 optimizer 的写法完全对齐。
        self.optimizer = self.optimizer_class(
            self.parameters(), lr=lr_schedule(1), **self.optimizer_kwargs
        )

    def _zero_init_adapters(self):
        """末层置零 → 阶段二起点严格等于阶段一策略，避免随机旁路瞬间摧毁预训练成果。"""
        for head in (self.actor_adapter[-1], self.critic_adapter[-1]):
            nn.init.zeros_(head.weight)
            nn.init.zeros_(head.bias)

    def _fuse(self, obs: Dict[str, torch.Tensor]) -> Tuple[torch.Tensor, torch.Tensor]:
        """主干前向 + 旁路融合，返回 (logits, values)。三个覆盖方法共用，保证一致。"""
        numeric = self.features_extractor(obs)              # numeric(13)
        latent_pi, latent_vf = self.mlp_extractor(numeric)
        logits = self.action_net(latent_pi)   # actor(演员)：输出 5 档仓位的动作倾向
        values = self.value_net(latent_vf)    # critic(评论家)：输出状态价值 V(s)，作优势 A 的基准线
        if self.use_adapter:
            # 旁路只吃原始 Dict 的 llm 分支（不经 preprocess）；关掉即退化回冻结主干（消融用）
            llm = obs["llm"]
            logits = logits + self.actor_adapter(llm)
            values = values + self.critic_adapter(llm)
        return logits, values

    def forward(self, obs, deterministic: bool = False):
        logits, values = self._fuse(obs)
        distribution = self.action_dist.proba_distribution(action_logits=logits)
        actions = distribution.get_actions(deterministic=deterministic)
        log_probs = distribution.log_prob(actions)
        return actions, values, log_probs

    def get_distribution(self, obs: Dict[str, torch.Tensor]):
        """
        覆盖：动作分布必须由 _fuse（含旁路）给出。

        为什么必须覆盖：SB3 推理链 predict → _predict → get_distribution().get_actions()
        【不走 forward】。父类 get_distribution 用 extract_features→_get_action_dist_from_latent，
        只算 action_net(latent_pi)、丢掉旁路 → 部署/回测/消融时 LLM 不参与决策，
        与训练（forward 含旁路）不一致。覆盖后推理与训练同走 _fuse，旁路真正生效。
        """
        logits, _ = self._fuse(obs)
        return self.action_dist.proba_distribution(action_logits=logits)

    def evaluate_actions(self, obs, actions):
        # SB3 2.9 契约：返回 (values, log_prob, entropy) 三元组，train() 按此解包
        logits, values = self._fuse(obs)
        distribution = self.action_dist.proba_distribution(action_logits=logits)
        log_probs = distribution.log_prob(actions)
        entropy = distribution.entropy()
        return values, log_probs, entropy

    def predict_values(self, obs) -> torch.Tensor:
        _, values = self._fuse(obs)
        return values

    # ---------- 阶段二训练控制 ----------

    def freeze_backbone(self) -> List[nn.Parameter]:
        """
        冻结主干、仅旁路可训（只切 requires_grad，不重建 optimizer）。

        为什么不重建 optimizer（曾这样做，是个坑）：
          1. 没必要——SB3 默认 Adam(weight_decay=0)，且 PyTorch Adam.step() 对 p.grad is None
             的参数直接跳过；主干 requires_grad=False 后 backward 不产生梯度，Adam 自然不更新它。
          2. 有害——重建后 optimizer 只剩旁路参数组，存盘的 optimizer 状态与「重新构造的完整
             policy」参数组大小不一致，PPO.load 会抛 "parameter group doesn't match"，
             导致训好的阶段二模型无法被下游回测加载。
        学习率由 PPO(learning_rate=...) 统一控制，无需在此设置。
        """
        for name, param in self.named_parameters():
            param.requires_grad = "adapter" in name
        return [p for n, p in self.named_parameters() if "adapter" in n]


def load_stage1_backbone(stage2_policy: ResidualLLMPolicy,
                         stage1_policy: ActorCriticPolicy) -> Tuple[List[str], List[str]]:
    """
    把阶段一主干权重迁移进阶段二 ResidualLLMPolicy。

    主干 key（features_extractor/mlp_extractor/action_net/value_net）两阶段完全一致；
    旁路 key 不在阶段一 state_dict 中 → strict=False 时它们保持 zero-init（不被覆盖）。

    返回 (missing_keys, unexpected_keys) 供调用方校验：
      missing 应为全部 adapter.* 参数（预期内）；unexpected 应为空（否则结构不匹配）。
    """
    stage1_sd = stage1_policy.state_dict()
    missing, unexpected = stage2_policy.load_state_dict(stage1_sd, strict=False)
    return list(missing), list(unexpected)
