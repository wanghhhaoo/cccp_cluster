"""
Expert: 同构自分化跟踪专家网络

延续 PKCI 的同构设计——所有专家结构完全相同, 通过对称性打破
机制 (负载均衡 + 多样性正则化 + 不同初始化) 自分化出不同专精方向。

潜在涌现专家方向 (运行后才能确认):
- 小目标专家
- 长时遮挡专家
- 相似干扰专家
- 快速运动专家
- 形变/旋转专家

专家输入:
    fused_feature  [B, C, H, W]   backbone 输出的搜索区域特征图
    template_vec   [B, C]         模板向量
专家输出:
    refined_bbox   [B, 4]   bbox 精化结果 (cx, cy, w, h)
    reid_embed     [B, D]   ReID embedding (D=128 默认)
    confidence     [B]      该专家自评的可靠性
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import List, Optional


class TrackingExpert(nn.Module):
    """
    单个跟踪专家。

    结构:
      1. Cross-attention: search_feat × template_vec → 加权 search 特征
      2. bbox refinement head: 输出 4 维 bbox 偏移
      3. ReID head: 输出判别性 embedding (L2 normalized)
      4. uncertainty head: 输出标量自信度

    与 PKCI 的 ExpertHead 不同点:
    - 输入是 2D 特征图而不是 1D 向量 (跟踪是空间任务)
    - 输出 bbox + ReID 而不是 logits
    - 多了一个 cross-attention 步骤实现模板-搜索匹配
    """

    def __init__(
        self,
        embed_dim: int = 192,
        feat_size: int = 16,
        hidden_dim: int = 256,
        reid_dim: int = 128,
        expert_id: int = 0,
        correction_scale: float = 0.25,
    ):
        super().__init__()
        self.expert_id = expert_id
        self.reid_dim = reid_dim
        # 专家只输出"有界修正": delta = correction_scale * tanh(raw) * [w,h,w,h].
        # 限幅保证再差的专家也只能微调 anchor、拽不飞 (配合卡尔曼平滑抗抖动).
        self.correction_scale = correction_scale

        # template 投影 → cross-attention key/value
        self.t_proj = nn.Linear(embed_dim, hidden_dim)
        self.s_proj = nn.Conv2d(embed_dim, hidden_dim, kernel_size=1)

        # cross-attention (单层简化版)
        self.cross_attn = nn.MultiheadAttention(
            hidden_dim, num_heads=4, batch_first=True,
        )
        self.norm = nn.LayerNorm(hidden_dim)

        # bbox refinement: 把全局池化后的特征 → 4 维偏移
        self.bbox_pool = nn.AdaptiveAvgPool2d(1)
        self.bbox_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(0.1),
            nn.Linear(hidden_dim, 4),
        )

        # ReID embedding head
        self.reid_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_dim, reid_dim),
        )

        # uncertainty head
        self.uncertainty_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid(),
        )

    def forward(
        self,
        search_feat: torch.Tensor,
        template_vec: torch.Tensor,
        coarse_bbox: Optional[torch.Tensor] = None,
    ) -> dict:
        """
        Args:
            search_feat:  [B, C, H, W]
            template_vec: [B, C]
            coarse_bbox:  [B, 4] (可选, 跟踪头已给出的粗 bbox, 提供给专家做 refinement)

        Returns:
            dict:
                delta:        [B, 4] 有界修正量 (cx, cy, w, h)
                refined_bbox: [B, 4] = coarse_bbox + delta (兼容旧调用方)
                reid_embed:   [B, reid_dim] L2 normalized
                confidence:   [B]
                expert_id:    int
        """
        B, C, H, W = search_feat.shape

        # 投影 search 特征图为 token 序列
        s = self.s_proj(search_feat)                    # [B, hidden, H, W]
        s_tokens = s.flatten(2).transpose(1, 2)         # [B, H*W, hidden]

        # template 作为 query
        t = self.t_proj(template_vec).unsqueeze(1)      # [B, 1, hidden]

        # cross-attention: template 询问 search, 返回与模板最相关的特征
        attended, _ = self.cross_attn(t, s_tokens, s_tokens, need_weights=False)
        # 也聚合 search 自己的全局表征
        s_pooled = s_tokens.mean(dim=1, keepdim=True)   # [B, 1, hidden]
        feat = self.norm(attended + s_pooled).squeeze(1)  # [B, hidden]

        # bbox: 输出 4 维原始量 → tanh 限幅 → 按 coarse_bbox 尺度缩放成有界修正
        raw = self.bbox_head(feat)                      # [B, 4] 无界
        if coarse_bbox is not None:
            # 每个分量按目标尺寸缩放: cx 按 w, cy 按 h, w 按 w, h 按 h
            scale = torch.stack([
                coarse_bbox[:, 2], coarse_bbox[:, 3],
                coarse_bbox[:, 2], coarse_bbox[:, 3],
            ], dim=-1)                                  # [B, 4] = [w,h,w,h]
            delta = self.correction_scale * torch.tanh(raw) * scale
            refined_bbox = coarse_bbox + delta
        else:
            # 无 coarse 时退化为固定尺度的有界修正
            delta = self.correction_scale * torch.tanh(raw) * 32.0
            refined_bbox = delta

        # ReID embedding (L2 normalized 用于 cosine 相似度)
        reid = self.reid_head(feat)                     # [B, reid_dim]
        reid = F.normalize(reid, dim=-1)

        # confidence
        conf = self.uncertainty_head(feat).squeeze(-1)  # [B]

        return {
            "delta": delta,
            "refined_bbox": refined_bbox,
            "reid_embed": reid,
            "confidence": conf,
            "expert_id": self.expert_id,
        }


class ExpertPool(nn.Module):
    """
    单架无人机上的同构专家池。

    与 PKCI ExpertPool 的差异:
    - PKCI 每架无人机 1 通用 + 1 特定 (异构, 命名区分);
    - PKCT 每架无人机 N 个同构专家 (默认 4), 通过自分化区分。
      远程请求时按 expert_id 索引, 而不是 expert_type 字符串。
    """

    def __init__(
        self,
        num_experts: int = 3,
        embed_dim: int = 192,
        feat_size: int = 16,
        hidden_dim: int = 256,
        reid_dim: int = 128,
    ):
        super().__init__()
        # num_experts 个"专精专家"(各对应一种困境) + 1 个"共享专家"(始终施加).
        # 类比 DMoE 的 shared+specialist: 共享专家 = anchor 之上的通用修正,
        # 专精专家 = 由 regime 软门控的困境特定修正.
        self.num_experts = num_experts
        self.experts = nn.ModuleList([
            TrackingExpert(
                embed_dim=embed_dim,
                feat_size=feat_size,
                hidden_dim=hidden_dim,
                reid_dim=reid_dim,
                expert_id=i,
            )
            for i in range(num_experts)
        ])
        self.shared_expert = TrackingExpert(
            embed_dim=embed_dim, feat_size=feat_size,
            hidden_dim=hidden_dim, reid_dim=reid_dim, expert_id=-1,
        )
        # 不同初始化种子打破对称性 (专精专家之间)
        self._init_with_different_seeds()

    def _init_with_different_seeds(self):
        for i, expert in enumerate(self.experts):
            generator = torch.Generator().manual_seed(42 + i * 17)
            for p in expert.parameters():
                if p.dim() >= 2:
                    try:
                        nn.init.kaiming_normal_(p, generator=generator)
                    except TypeError:   # torch < 2.2 无 generator 参数; 推理时权重会被 checkpoint 覆盖
                        nn.init.kaiming_normal_(p)

    def assemble(
        self,
        search_feat: torch.Tensor,
        template_vec: torch.Tensor,
        coarse_bbox: torch.Tensor,
        regime_weights: torch.Tensor,
    ) -> dict:
        """
        软组装专家修正: corrected = coarse + δ_shared + Σ_k a_k · δ_k.

        Args:
            coarse_bbox:     [B, 4] anchor 给出的基础 bbox (cx, cy, w, h)
            regime_weights:  [B, 6] softmax over {normal, 5 困境}.
                             第 0 维 (normal) 不乘任何专精专家 → 简单帧修正自动趋零;
                             共享专家始终施加 (与 normal 无关).
        Returns:
            corrected_bbox / delta / reid_embed / confidence / specialists / shared
        """
        shared = self.shared_expert(search_feat, template_vec, coarse_bbox)
        specialists = [
            e(search_feat, template_vec, coarse_bbox) for e in self.experts
        ]
        # 专精权重 = regime 的后 num_experts 维 (跳过 index 0 的 normal)
        w = regime_weights[:, 1:1 + self.num_experts]          # [B, num_experts]

        delta = shared["delta"]
        reid = shared["reid_embed"]
        for k in range(self.num_experts):
            wk = w[:, k:k + 1]                                 # [B, 1]
            delta = delta + wk * specialists[k]["delta"]
            reid = reid + wk * specialists[k]["reid_embed"]
        corrected_bbox = coarse_bbox + delta
        reid = F.normalize(reid, dim=-1)

        return {
            "corrected_bbox": corrected_bbox,
            "delta": delta,
            "reid_embed": reid,
            "confidence": shared["confidence"],
            "specialists": specialists,
            "shared": shared,
        }

    def forward_local(
        self,
        search_feat: torch.Tensor,
        template_vec: torch.Tensor,
        coarse_bbox: Optional[torch.Tensor] = None,
        expert_idx: int = 0,
    ) -> dict:
        """本地推理: 用指定的一个专家 (默认 expert 0). 最快路径."""
        return self.experts[expert_idx](search_feat, template_vec, coarse_bbox)

    def forward_expert(
        self,
        search_feat: torch.Tensor,
        template_vec: torch.Tensor,
        coarse_bbox: Optional[torch.Tensor] = None,
        expert_idx: int = 0,
    ) -> dict:
        """指定专家推理: 被远程节点请求时使用."""
        if 0 <= expert_idx < self.num_experts:
            return self.experts[expert_idx](search_feat, template_vec, coarse_bbox)
        return self.experts[0](search_feat, template_vec, coarse_bbox)

    def forward_all(
        self,
        search_feat: torch.Tensor,
        template_vec: torch.Tensor,
        coarse_bbox: Optional[torch.Tensor] = None,
    ) -> List[dict]:
        """所有专家并行前向 (用于训练时的 router 监督)."""
        return [e(search_feat, template_vec, coarse_bbox) for e in self.experts]

    def get_available_experts(self) -> List[int]:
        return list(range(self.num_experts))
