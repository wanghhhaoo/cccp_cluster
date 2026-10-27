"""
ReIDFusion: 多视角 ReID embedding 融合

ReID embedding 用于:
  1. 与初始模板的相似度判断 (是否还在跟踪正确目标)
  2. 长时丢失后的 re-detection
  3. 跨视角的目标关联确认
  4. 几何信息不可靠时的 fallback 融合策略 (策略 B)

融合公式 (置信度加权 + L2 归一化):
    e_fused = normalize( Σ_k w_k * e_k )

w_k 由该视角的可见性、置信度、视角因子共同决定.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import List, Optional


class ReIDFusion(nn.Module):
    """
    ReID embedding 融合模块.

    与 KalmanFusion 互补:
    - KalmanFusion 处理 bbox 几何状态 (主路径)
    - ReIDFusion 处理外观特征 (验证 + fallback)
    """

    def __init__(self, reid_dim: int = 128):
        super().__init__()
        self.reid_dim = reid_dim

    def fuse(
        self,
        local_embed: torch.Tensor,
        local_confidence: float,
        remote_embeds: List[torch.Tensor],
        remote_confidences: List[float],
        view_angle_factors: Optional[List[float]] = None,
    ) -> torch.Tensor:
        """
        加权平均融合多视角 ReID.

        Args:
            local_embed:        [reid_dim] L2 normalized
            local_confidence:   [0, 1]
            remote_embeds:      list of [reid_dim]
            remote_confidences: list of float
            view_angle_factors: list of float (默认 1.0)

        Returns:
            fused_embed: [reid_dim] L2 normalized
        """
        if view_angle_factors is None:
            view_angle_factors = [1.0] * len(remote_embeds)

        target_dim = local_embed.shape[-1]
        weighted_sum = local_embed * local_confidence
        weight_total = local_confidence

        for e, c, va in zip(remote_embeds, remote_confidences, view_angle_factors):
            # 维度不匹配时 zero-pad 到 local 维度.
            # (带宽压缩可能把 reid 截到 16/32/64; pad 表示 "未传输的维度未知")
            if e.shape[-1] != target_dim:
                if e.shape[-1] < target_dim:
                    pad = torch.zeros(target_dim - e.shape[-1], device=e.device,
                                      dtype=e.dtype)
                    e = torch.cat([e, pad], dim=-1)
                else:
                    e = e[..., :target_dim]
            # 视角越好 (va 越小) 权重越高
            w = c / max(va, 0.1)
            weighted_sum = weighted_sum + e * w
            weight_total += w

        fused = weighted_sum / max(weight_total, 1e-6)
        fused = F.normalize(fused, dim=-1)
        return fused

    def cosine_similarity(
        self, embed_a: torch.Tensor, embed_b: torch.Tensor,
    ) -> torch.Tensor:
        """计算 cosine 相似度. 维度不匹配时取共同前缀 + 重新归一化.

        带宽自适应压缩会把远程 reid 截到更小维度 (16/32/64), 与本地 128 维
        无法直接点乘. 我们在共同前缀上做 cosine, 这是 PCA-like 投影下的
        近似一致性度量.
        """
        if embed_a.shape[-1] == embed_b.shape[-1]:
            return (embed_a * embed_b).sum(dim=-1)
        d = min(embed_a.shape[-1], embed_b.shape[-1])
        a = F.normalize(embed_a[..., :d], dim=-1)
        b = F.normalize(embed_b[..., :d], dim=-1)
        return (a * b).sum(dim=-1)

    def template_similarity(
        self,
        current_embed: torch.Tensor,
        template_embeds: List[torch.Tensor],
    ) -> dict:
        """
        当前 embedding 与模板库 (多个时刻的高质量 embedding) 的相似度.

        用于:
        - 漂移检测: max_sim < 阈值 → 怀疑跟错目标
        - lost recovery: 长时丢失后用 max_sim 判断是否找回
        """
        sims = [self.cosine_similarity(current_embed, t) for t in template_embeds]
        sim_tensor = torch.stack(sims)
        return {
            "max_sim": float(sim_tensor.max().item()),
            "mean_sim": float(sim_tensor.mean().item()),
            "min_sim": float(sim_tensor.min().item()),
            "all_sims": sim_tensor,
        }

    def consistency_check(
        self,
        local_embed: torch.Tensor,
        remote_embeds: List[torch.Tensor],
        threshold: float = 0.6,
    ) -> dict:
        """
        多视角一致性检查: 检查所有远程 embedding 是否还在描述同一目标.

        如果某个远程 embedding 与本地相似度过低, 该视角可能跟错了目标,
        融合时应排除或降权.
        """
        if not remote_embeds:
            return {"consistent": True, "valid_indices": [], "sims": []}

        sims = [self.cosine_similarity(local_embed, e).item() for e in remote_embeds]
        valid = [i for i, s in enumerate(sims) if s >= threshold]
        return {
            "consistent": len(valid) == len(sims),
            "valid_indices": valid,
            "sims": sims,
        }
