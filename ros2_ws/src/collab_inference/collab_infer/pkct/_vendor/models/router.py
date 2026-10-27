"""
Router: 时序感知三层路由器

跟踪任务路由器决策比分类更复杂, 因为"困难状态"判断需要时序信息:
  - 本地 confidence + 时序退化趋势
  - 目标在搜索区域中的相对位置 (接近边界提示需要协同)
  - ReID 与初始模板的相似度

输出三层决策:
  1. 是否需要协同 (need_collab)
  2. 请求哪类协同 (collab_type ∈ {bbox_calib, reid_verify, full_search})
  3. 协同紧迫性 (urgency ∈ [0,1])

紧迫性低 → 超时即跳过; 紧迫性高 → 等更多增量, 容忍更长延迟.
"""

import torch
import torch.nn as nn
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class TrackingRoutingDecision:
    """跟踪场景的路由决策结果."""
    need_collaboration: bool
    collab_type: str                   # "bbox_calib" / "reid_verify" / "full_search" / "none"
    urgency: float                     # [0, 1]
    target_drones: List[int]           # 期望请求的邻居 drone_id 列表
    target_expert_ids: List[int]       # 期望的专家 id (跨视角融合通常请求多个)
    reason: str                        # 触发原因 (调试用)


class TemporalRouter(nn.Module):
    """
    时序感知路由器.

    输入特征:
      - feature_vec:        [B, F]     backbone 全局特征
      - local_confidence:   [B]        本地跟踪头置信度
      - conf_history:       [B, T]     最近 T 帧的 confidence 序列
      - bbox_in_search:     [B, 4]     当前 bbox 在搜索区域内的相对坐标 (归一化)
      - reid_template_sim:  [B]        当前 reid 与模板 reid 的 cosine 相似度
    """

    COLLAB_TYPES = ["none", "bbox_calib", "reid_verify", "full_search"]

    # 困境路由档位, 与 data/difficulty_augment.DIFFICULTY_TYPES 对齐:
    # index 0 = normal (无困境, 不触发专精专家), 1..N = N 种"可见但难"的困境.
    # v2: 去掉 occlusion/distractor (归卡尔曼/时序兜底 + 协同), 只留"多算力有用"的纯 refinement 困境.
    REGIMES = ["normal", "small", "motion_blur", "deform"]

    def __init__(
        self,
        feature_dim: int = 256,
        history_len: int = 5,
        hidden: int = 128,
    ):
        super().__init__()
        self.feature_dim = feature_dim
        self.history_len = history_len

        # 困境路由头 (单视角 MoE 用): backbone 全局特征 → 6 维 regime 分数.
        # 由 difficulty_augment 的困境标签监督 (P2), 软分数门控专精专家.
        self.regime_head = nn.Sequential(
            nn.Linear(feature_dim, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, len(self.REGIMES)),
        )

        # 输入: feature_vec + local_conf + conf_history + bbox_in_search + reid_sim
        in_dim = feature_dim + 1 + history_len + 4 + 1

        # 1. need_collab head (binary)
        self.need_collab_head = nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, 1),
            nn.Sigmoid(),
        )

        # 2. collab_type head (4-way classifier)
        self.collab_type_head = nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, len(self.COLLAB_TYPES)),
        )

        # 3. urgency head (regression in [0,1])
        self.urgency_head = nn.Sequential(
            nn.Linear(in_dim, hidden // 2),
            nn.ReLU(inplace=True),
            nn.Linear(hidden // 2, 1),
            nn.Sigmoid(),
        )

    def _build_input(
        self,
        feature_vec: torch.Tensor,
        local_confidence: torch.Tensor,
        conf_history: Optional[torch.Tensor],
        bbox_in_search: torch.Tensor,
        reid_template_sim: torch.Tensor,
    ) -> torch.Tensor:
        B = feature_vec.shape[0]
        device = feature_vec.device

        if conf_history is None:
            conf_history = torch.zeros(B, self.history_len, device=device)
        elif conf_history.size(1) < self.history_len:
            pad = torch.zeros(B, self.history_len - conf_history.size(1), device=device)
            conf_history = torch.cat([pad, conf_history], dim=-1)
        elif conf_history.size(1) > self.history_len:
            conf_history = conf_history[:, -self.history_len:]

        return torch.cat([
            feature_vec,
            local_confidence.unsqueeze(-1),
            conf_history,
            bbox_in_search,
            reid_template_sim.unsqueeze(-1),
        ], dim=-1)

    def forward(
        self,
        feature_vec: torch.Tensor,
        local_confidence: torch.Tensor,
        conf_history: Optional[torch.Tensor] = None,
        bbox_in_search: Optional[torch.Tensor] = None,
        reid_template_sim: Optional[torch.Tensor] = None,
    ) -> dict:
        """
        Args:
            feature_vec:        [B, F]
            local_confidence:   [B]
            conf_history:       [B, T] 或 None
            bbox_in_search:     [B, 4] 归一化坐标 [cx,cy,w,h]/search_size 或 None
            reid_template_sim:  [B] 或 None
        Returns:
            dict with need_collab, collab_type_probs, urgency
        """
        B = feature_vec.shape[0]
        device = feature_vec.device

        if bbox_in_search is None:
            bbox_in_search = torch.full((B, 4), 0.5, device=device)
        if reid_template_sim is None:
            reid_template_sim = torch.ones(B, device=device)

        x = self._build_input(
            feature_vec, local_confidence,
            conf_history, bbox_in_search, reid_template_sim,
        )

        need_collab = self.need_collab_head(x).squeeze(-1)          # [B]
        collab_type_logits = self.collab_type_head(x)               # [B, 4]
        collab_type_probs = torch.softmax(collab_type_logits, dim=-1)
        urgency = self.urgency_head(x).squeeze(-1)                  # [B]

        return {
            "need_collab": need_collab,
            "collab_type_probs": collab_type_probs,
            "collab_type_logits": collab_type_logits,
            "urgency": urgency,
        }

    def forward_regime(self, feature_vec: torch.Tensor) -> dict:
        """
        困境路由 (单视角 MoE): backbone 全局特征 → regime 软分数.

        Args:
            feature_vec: [B, feature_dim]
        Returns:
            regime_logits:  [B, 6]  (供 CE 监督)
            regime_weights: [B, 6]  softmax, 用于门控专精专家 (见 ExpertPool.assemble)
        """
        logits = self.regime_head(feature_vec)
        return {
            "regime_logits": logits,
            "regime_weights": torch.softmax(logits, dim=-1),
        }

    def make_routing_decision(
        self,
        router_output: dict,
        neighbor_status: Optional[Dict[int, dict]] = None,
        max_targets: int = 3,
        need_collab_threshold: float = 0.4,
    ) -> TrackingRoutingDecision:
        """
        将 forward() 输出转化为具体决策 (推理时用).

        Args:
            router_output:    forward() 返回 dict (B=1)
            neighbor_status:  {drone_id: {experts:[...], latency:.., load:..}}
            max_targets:      最多请求几个邻居
        """
        need_collab = router_output["need_collab"][0].item()
        collab_probs = router_output["collab_type_probs"][0]
        urgency = router_output["urgency"][0].item()

        if need_collab < need_collab_threshold:
            return TrackingRoutingDecision(
                need_collaboration=False,
                collab_type="none",
                urgency=urgency,
                target_drones=[],
                target_expert_ids=[],
                reason=f"need_collab={need_collab:.3f} below threshold",
            )

        # 选择最可能的 collab_type (排除 "none" 索引 0)
        type_idx = collab_probs[1:].argmax().item() + 1
        collab_type = self.COLLAB_TYPES[type_idx]

        # 选择目标邻居 (按延迟 + 负载排序)
        target_drones: List[int] = []
        target_expert_ids: List[int] = []
        if neighbor_status:
            scored = []
            for did, status in neighbor_status.items():
                latency = status.get("latency", 0.0)
                load = status.get("load", 0.0)
                # 越低越好
                cost = latency + load * 50.0
                scored.append((cost, did, status.get("experts", [0])))
            scored.sort(key=lambda x: x[0])
            for cost, did, experts in scored[:max_targets]:
                target_drones.append(did)
                target_expert_ids.append(experts[0] if experts else 0)

        return TrackingRoutingDecision(
            need_collaboration=True,
            collab_type=collab_type,
            urgency=urgency,
            target_drones=target_drones,
            target_expert_ids=target_expert_ids,
            reason=(
                f"need_collab={need_collab:.3f}, "
                f"type={collab_type}, urgency={urgency:.3f}"
            ),
        )
