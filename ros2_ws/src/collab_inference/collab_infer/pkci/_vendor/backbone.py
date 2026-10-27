"""
Backbone: 共享主干网络
所有无人机部署相同的backbone，负责特征提取。
输出: feature vector + 本地粗分类结果
"""

import torch
import torch.nn as nn
import torchvision.models as models


class Backbone(nn.Module):
    """
    轻量主干网络，基于EfficientNet-B0。

    输出两样东西:
    1. feature_vec: 中间特征向量 (用于路由决策和远程专家输入)
    2. local_logits: 本地粗分类结果 (fast path直接可用)
    """

    def __init__(self, num_classes: int = 100, feature_dim: int = 512):
        super().__init__()
        self.feature_dim = feature_dim
        self.num_classes = num_classes

        # --- 特征提取器: EfficientNet-B0 去掉分类头 ---
        efficientnet = models.efficientnet_b0(weights=None)
        # 取出特征提取部分 (卷积 + pooling)
        self.features = efficientnet.features
        self.pool = nn.AdaptiveAvgPool2d(1)

        # trunk/top 分叉点: features[:7]=trunk(共享, 2.88M, 输出 [B,192,7,7]);
        # features[7:]=top(1.13M, 每个 AdapterExpert 克隆一份作专精顶部块)。
        # 仅拆 forward 调用, 不改子模块/参数名 -> 老 checkpoint 1:1 加载。
        self.trunk_end = 7

        # EfficientNet-B0 最后一层输出 1280 维
        effnet_dim = 1280

        # --- 特征映射: 1280 -> feature_dim ---
        self.feature_proj = nn.Sequential(
            nn.Linear(effnet_dim, feature_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(0.2),
        )

        # --- 本地快速分类头: feature_dim -> num_classes ---
        # 这是fast path，保证离线时也能分类
        self.local_classifier = nn.Sequential(
            nn.Linear(feature_dim, feature_dim // 2),
            nn.ReLU(inplace=True),
            nn.Dropout(0.3),
            nn.Linear(feature_dim // 2, num_classes),
        )

    def forward_trunk(self, x: torch.Tensor) -> torch.Tensor:
        """共享 trunk: 输出 pool 前的中间特征图 [B, 192, 7, 7]。
        AdapterExpert 吃这个 (访问空间信息), general 路径继续走 forward_top。"""
        h = x
        for i in range(self.trunk_end):
            h = self.features[i](h)
        return h

    def forward_top(self, trunk_map: torch.Tensor) -> dict:
        """顶部块 + pool + proj + 本地分类头 (general/local 路径)。"""
        h = trunk_map
        for i in range(self.trunk_end, len(self.features)):
            h = self.features[i](h)
        h = self.pool(h).flatten(1)                # [B, 1280]
        feature_vec = self.feature_proj(h)         # [B, feature_dim]
        local_logits = self.local_classifier(feature_vec)
        local_confidence = torch.softmax(local_logits, dim=-1).max(dim=-1).values
        return {
            "feature": feature_vec,
            "local_logits": local_logits,
            "local_confidence": local_confidence,
        }

    def top_block(self) -> nn.Module:
        """返回顶部块 features[trunk_end:] 的引用, 供 AdapterExpert 克隆 warm-start。"""
        return self.features[self.trunk_end:]

    def forward(self, x: torch.Tensor) -> dict:
        """完整前向 = forward_top(forward_trunk(x))。与拆分前逐比特一致。
        Returns: dict('feature' [B,feature_dim], 'local_logits' [B,num_classes], 'local_confidence' [B])
        """
        return self.forward_top(self.forward_trunk(x))


class BackboneLite(nn.Module):
    """
    更轻量的backbone变体，用MobileNetV3-Small。
    适用于算力更受限的场景或需要更低延迟时。
    """

    def __init__(self, num_classes: int = 100, feature_dim: int = 256):
        super().__init__()
        self.feature_dim = feature_dim

        mobilenet = models.mobilenet_v3_small(weights=None)
        self.features = mobilenet.features
        self.pool = nn.AdaptiveAvgPool2d(1)

        # MobileNetV3-Small 输出 576 维
        self.feature_proj = nn.Sequential(
            nn.Linear(576, feature_dim),
            nn.ReLU(inplace=True),
        )

        self.local_classifier = nn.Sequential(
            nn.Linear(feature_dim, num_classes),
        )

    def forward(self, x: torch.Tensor) -> dict:
        feat = self.features(x)
        feat = self.pool(feat).flatten(1)
        feature_vec = self.feature_proj(feat)
        local_logits = self.local_classifier(feature_vec)
        local_probs = torch.softmax(local_logits, dim=-1)

        return {
            "feature": feature_vec,
            "local_logits": local_logits,
            "local_confidence": local_probs.max(dim=-1).values,
        }
