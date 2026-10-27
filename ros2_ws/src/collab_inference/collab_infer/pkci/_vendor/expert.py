"""
Expert: 专家网络
每个专家是backbone之上的一个专精分类头，针对特定困难模式优化。
专家不输出最终类别，而是输出"知识向量"(calibrated logits)。
"""

import copy
import torch
import torch.nn as nn
from enum import Enum
from typing import Optional


class ExpertType(Enum):
    """专家类型 - 按分类困难模式划分"""
    GENERAL = "general"            # 通用专家 (每架无人机都有)
    LOW_LIGHT = "low_light"        # 低光照/逆光
    SIMILAR_CLASS = "similar_class" # 细粒度相似类别
    MOTION_BLUR = "motion_blur"    # 运动模糊
    OCCLUSION = "occlusion"        # 遮挡


class ExpertHead(nn.Module):
    """
    单个专家分类头。

    设计要点:
    - 输入是backbone的feature vector，不是原图
    - 输出是calibrated logits，不是类别
    - 包含一个不确定性估计头，用于融合时的权重分配
    """

    def __init__(
        self,
        feature_dim: int = 512,
        num_classes: int = 100,
        expert_type: ExpertType = ExpertType.GENERAL,
        hidden_dim: int = 256,
    ):
        super().__init__()
        self.expert_type = expert_type
        self.num_classes = num_classes

        # --- 主分类路径 ---
        self.classifier = nn.Sequential(
            nn.Linear(feature_dim, hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(0.3),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(0.2),
            nn.Linear(hidden_dim, num_classes),
        )

        # --- 不确定性估计头 ---
        # 输出一个标量，表示该专家对当前输入的自信程度
        # 用于融合模块中的权重分配
        self.uncertainty_head = nn.Sequential(
            nn.Linear(feature_dim, hidden_dim // 2),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid(),  # 输出 [0, 1]，1 = 非常自信
        )

        # --- 温度校准参数 ---
        # 可学习的温度参数，用于校准 logits 的分布
        self.temperature = nn.Parameter(torch.ones(1) * 1.5)

    def forward(self, feature: torch.Tensor) -> dict:
        """
        Args:
            feature: backbone输出的特征向量 [B, feature_dim]
        Returns:
            dict with:
                - 'logits': 校准后的分类logits [B, num_classes]
                - 'confidence': 专家自信度 [B]
                - 'expert_type': 专家类型标识
        """
        raw_logits = self.classifier(feature)

        # 温度校准: 让logits分布更合理，避免过度自信
        calibrated_logits = raw_logits / self.temperature.clamp(min=0.1)

        # 不确定性/自信度估计
        confidence = self.uncertainty_head(feature).squeeze(-1)  # [B]

        return {
            "logits": calibrated_logits,
            "raw_logits": raw_logits,
            "confidence": confidence,
            "expert_type": self.expert_type,
        }


class AdapterExpert(nn.Module):
    """
    专家 = 共享 trunk 之上的"顶部块副本 + 分类头"，吃 trunk 的中间特征图。

    比 ExpertHead (pooled 512 上的弱 MLP) 强在: 有真实卷积容量 + 访问 pool 前
    的空间特征 → 能在困难条件 (blur/occlusion) 上真正抽取信息 (突破 ≤2pp 天花板)。
    warm-start: top/proj 从 backbone 顶部拷、head 从 general 专家拷 → 初始输出
    ≈ general (下限稳: 未充分专精时也不会比 local 差)。

    注意: 输入是 trunk 特征图 (backbone.forward_trunk(x)), 不是 pooled feature。
    """

    def __init__(
        self,
        top_template: nn.Module,
        proj_template: nn.Module,
        feature_dim: int = 512,
        num_classes: int = 100,
        expert_type: ExpertType = ExpertType.GENERAL,
        hidden_dim: int = 256,
    ):
        super().__init__()
        self.expert_type = expert_type
        self.num_classes = num_classes
        self.top = copy.deepcopy(top_template)      # features[trunk_end:] 副本
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.proj = copy.deepcopy(proj_template)    # feature_proj 副本
        self.head = ExpertHead(feature_dim, num_classes, expert_type, hidden_dim)

    def forward(self, trunk_map: torch.Tensor) -> dict:
        h = self.top(trunk_map)
        h = self.pool(h).flatten(1)
        feat = self.proj(h)
        return self.head(feat)

    @torch.no_grad()
    def warm_start(self, top_module: nn.Module, proj_module: nn.Module, general_head: "ExpertHead"):
        """从 (已训练的) backbone 顶部 + general 专家 拷权重 → 初始 forward(map) ≈ general(feature)。"""
        self.top.load_state_dict(top_module.state_dict())
        self.proj.load_state_dict(proj_module.state_dict())
        self.head.load_state_dict(general_head.state_dict())


class ExpertPool(nn.Module):
    """
    单架无人机上的专家池。

    每架无人机包含:
    - 1个通用专家 (必有, 恒为 ExpertHead)
    - 1个特定专家 (可选; use_adapter=True 时用 AdapterExpert, 否则 ExpertHead)
    """

    def __init__(
        self,
        feature_dim: int = 512,
        num_classes: int = 100,
        local_expert_type: ExpertType = ExpertType.GENERAL,
        specialized_expert_type: Optional[ExpertType] = None,
        use_adapter: bool = False,
        top_template: Optional[nn.Module] = None,
        proj_template: Optional[nn.Module] = None,
    ):
        super().__init__()
        self.experts = nn.ModuleDict()
        self.use_adapter = use_adapter

        # 通用专家 (always present, 走 pooled feature)
        self.experts["general"] = ExpertHead(
            feature_dim, num_classes, ExpertType.GENERAL
        )

        # 特定专家 (optional)
        if specialized_expert_type and specialized_expert_type != ExpertType.GENERAL:
            if use_adapter:
                assert top_template is not None and proj_template is not None, \
                    "use_adapter=True 需传 backbone 的 top_template + proj_template"
                self.experts[specialized_expert_type.value] = AdapterExpert(
                    top_template, proj_template, feature_dim, num_classes, specialized_expert_type,
                )
            else:
                self.experts[specialized_expert_type.value] = ExpertHead(
                    feature_dim, num_classes, specialized_expert_type,
                )

        self.local_expert_type = local_expert_type
        self.specialized_expert_type = specialized_expert_type

    def forward_local(self, feature: torch.Tensor) -> dict:
        """本地推理: 只用通用专家 (ExpertHead on pooled feature), 最快路径"""
        return self.experts["general"](feature)

    def forward_expert(
        self, feature: torch.Tensor, expert_type: str,
        trunk_map: Optional[torch.Tensor] = None,
    ) -> dict:
        """指定专家推理: 被远程节点请求时使用。
        AdapterExpert 吃 trunk_map (中间特征图); ExpertHead 吃 pooled feature。"""
        expert = self.experts[expert_type] if expert_type in self.experts else self.experts["general"]
        if isinstance(expert, AdapterExpert):
            assert trunk_map is not None, "AdapterExpert 需要 trunk_map (backbone.forward_trunk 输出)"
            return expert(trunk_map)
        return expert(feature)

    def warm_start_adapters(self, backbone, general_head: Optional["ExpertHead"] = None):
        """把 (已加载的) backbone 顶部 + general 专家 拷进每个 AdapterExpert。
        老 checkpoint 加载后调用一次 → adapter 初始 ≈ general (等价性起点)。"""
        general_head = general_head or self.experts["general"]
        for et, expert in self.experts.items():
            if isinstance(expert, AdapterExpert):
                expert.warm_start(backbone.top_block(), backbone.feature_proj, general_head)

    def get_available_experts(self) -> list:
        """返回本节点可用的专家类型列表"""
        return list(self.experts.keys())
