"""
SelectiveTemporalMemory: 选择性时序记忆 (Mamba 思想, 非 Mamba 库)

设计文档 §4.1. 维护一个流式、定长的隐状态 h, 累积目标的外观/上下文随时间的演化,
并用它 (经 FiLM) 条件化当前帧的搜索特征 → 帮助定位 + 抗外观漂移/模板污染.

借鉴 Mamba 的两点:
  1. 状态空间递归: h_t = Ā·h_{t-1} + B̄·u_t, O(1) 每步、定长状态 → 流式、可导出.
  2. 选择性 (输入依赖 + quality 门控): 高置信帧强写入、遮挡/低置信帧弱写/不写
     → 在线模板更新 + 抗模板污染.
对角 SSM + 梯形离散化 (替代欧拉), 全逐元素 + 线性, 无 selective-scan 自定义核.

与卡尔曼分工: 时序记忆管"外观"(长啥样), 卡尔曼管"几何"(在哪/怎么动).

每帧因果顺序 (在 PKCTSystem 里):
    F~ = condition(F)            # 用上帧状态条件化 (读)
    head(F~) → bbox; e_t = ReID(F~ @ bbox)
    update(e_t, quality)         # 选择性写入 (写)
新视频开始调用 reset().
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional


class SelectiveTemporalMemory(nn.Module):
    """
    Args:
        in_dim:    输入观测维度 (= ReID embedding 维度)
        state_dim: 隐状态维度
        cond_dim:  被 FiLM 调制的特征通道数 (= backbone search_feature 的 C)
    """

    def __init__(self, in_dim: int = 128, state_dim: int = 128, cond_dim: int = 192):
        super().__init__()
        self.in_dim = in_dim
        self.state_dim = state_dim
        self.cond_dim = cond_dim

        # 选择性 SSM 参数 (对角)
        self.W_dt = nn.Linear(in_dim, state_dim)            # 输入依赖步长 Δ
        self.A_log = nn.Parameter(torch.zeros(state_dim))    # 衰减 A = -exp(A_log) < 0
        self.W_B = nn.Linear(in_dim, state_dim)              # 输入依赖 B
        self.W_in = nn.Linear(in_dim, state_dim)             # 输入投影 u

        # 读出 → FiLM(γ, β) 调制特征
        self.to_gamma = nn.Linear(state_dim, cond_dim)
        self.to_beta = nn.Linear(state_dim, cond_dim)

        self.h: Optional[torch.Tensor] = None  # [B, state_dim] 流式状态

    def reset(self):
        """新视频开始: 清空隐状态 (同时丢弃训练时的 BPTT 计算图)."""
        self.h = None

    def condition(self, search_feat: torch.Tensor) -> torch.Tensor:
        """
        用当前隐状态 (上一帧写入的) FiLM 调制 search_feat. 首帧 (h=None) 恒等返回.
        Args:  search_feat: [B, C, H, W]
        Returns: F~ [B, C, H, W]
        """
        if self.h is None:
            return search_feat
        gamma = 1.0 + torch.tanh(self.to_gamma(self.h))      # [B, C], 居中于 1
        beta = self.to_beta(self.h)                          # [B, C]
        return search_feat * gamma[:, :, None, None] + beta[:, :, None, None]

    def update(self, x_t: torch.Tensor, quality_t: torch.Tensor):
        """
        选择性写入: 由 quality 门控. 低 quality → Δ→0 → 状态几乎不变 (抗污染).
        Args:
            x_t:       [B, in_dim]  当前帧目标观测 (ReID embedding)
            quality_t: [B] 或 [B,1] 置信/质量 (∈[0,1])
        """
        B = x_t.shape[0]
        if quality_t.dim() == 1:
            quality_t = quality_t.unsqueeze(-1)              # [B,1]
        if self.h is None:
            self.h = x_t.new_zeros(B, self.state_dim)

        # 输入依赖步长, 由 quality 门控 (低 quality → 小 Δ → 不写)
        dt = F.softplus(self.W_dt(x_t)) * quality_t          # [B, state_dim]
        A = -torch.exp(self.A_log)                           # [state_dim] < 0
        # 梯形 (bilinear) 离散化: Ā = (1 + Δ·A/2)/(1 - Δ·A/2)
        dtA = dt * A
        A_bar = (1.0 + dtA / 2.0) / (1.0 - dtA / 2.0)        # [B, state_dim]
        B_t = self.W_B(x_t)                                  # [B, state_dim]
        u_t = self.W_in(x_t)                                 # [B, state_dim]
        self.h = A_bar * self.h + (dt * B_t) * u_t           # [B, state_dim]
