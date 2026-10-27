"""
TrackingHead: 三分支 CenterNet 风格跟踪头

输入: backbone 的 fused_feature [B, C, H, W]
输出三个分支:
  - cls (响应图):     [B, 1, H, W]  每个位置是目标中心的概率
  - reg (bbox 偏移):  [B, 4, H, W]  [l, t, r, b] 到 bbox 边界的距离
  - quality (质量):   [B, 1, H, W]  IoU 质量分数, 用于排序

后处理:
  1. 在 cls 上找峰值 → 候选中心点
  2. 在峰值位置读 reg → 候选 bbox
  3. quality 排序, 取最高 → 本地预测
  4. 输入卡尔曼滤波器做时序平滑
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Tuple


class CornerHead(nn.Module):
    """单个分支的 conv head."""

    def __init__(self, in_dim: int, out_dim: int, hidden: int = 128):
        super().__init__()
        self.body = nn.Sequential(
            nn.Conv2d(in_dim, hidden, kernel_size=3, padding=1),
            nn.BatchNorm2d(hidden),
            nn.ReLU(inplace=True),
            nn.Conv2d(hidden, hidden, kernel_size=3, padding=1),
            nn.BatchNorm2d(hidden),
            nn.ReLU(inplace=True),
            nn.Conv2d(hidden, out_dim, kernel_size=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.body(x)


class TrackingHead(nn.Module):
    """
    CenterNet 风格三分支跟踪头。

    Args:
        in_dim:        输入特征 channel (与 backbone embed_dim 一致)
        search_size:   搜索区域像素尺寸 (256)
        feat_stride:   特征图 stride (16)

    feat_size = search_size // feat_stride (16x16 默认)
    """

    def __init__(
        self,
        in_dim: int = 192,
        search_size: int = 256,
        feat_stride: int = 16,
        hidden_dim: int = 128,
    ):
        super().__init__()
        self.search_size = search_size
        self.feat_stride = feat_stride
        self.feat_size = search_size // feat_stride

        self.cls_head = CornerHead(in_dim, out_dim=1, hidden=hidden_dim)
        self.reg_head = CornerHead(in_dim, out_dim=4, hidden=hidden_dim)
        self.quality_head = CornerHead(in_dim, out_dim=1, hidden=hidden_dim)

        # bias reg_head's last conv so initial bbox is ~stride pixels wide,
        # not 1 pixel. without this, ReLU(small random values) → bbox≈0,
        # GIoU ≈ -1 forever. softplus(1.0) ≈ 1.31 gives bbox = 1.31 * stride
        # ≈ 21 px which roughly matches typical target sizes at init.
        with torch.no_grad():
            self.reg_head.body[-1].bias.fill_(1.0)

    def forward(self, search_feat: torch.Tensor) -> dict:
        """
        Args:
            search_feat: [B, C, H, W]
        Returns:
            cls_map:     [B, 1, H, W] 响应图 (sigmoid 前)
            reg_map:     [B, 4, H, W] [l, t, r, b]
            quality_map: [B, 1, H, W] IoU 质量分数 (sigmoid 前)
        """
        cls_map = self.cls_head(search_feat)
        # ltrb 必须为正; 用 softplus 替代 ReLU 避免梯度死区, 配合 bias=1.0 初始化
        # 让初始 bbox ~ stride 像素而非 0.
        reg_map = F.softplus(self.reg_head(search_feat))
        quality_map = self.quality_head(search_feat)
        return {
            "cls_map": cls_map,
            "reg_map": reg_map,
            "quality_map": quality_map,
        }

    @torch.no_grad()
    def decode_bbox(
        self,
        head_out: dict,
        last_bbox_in_search: Optional[torch.Tensor] = None,
    ) -> dict:
        """
        从三分支输出解码出最终 bbox (在 search 坐标系下)。

        Args:
            head_out:  forward() 返回的 dict
            last_bbox_in_search: 上一帧 bbox 在搜索区域内的位置 [B, 4]
                                 (可选, 用于汉宁窗加权抑制远离上次位置的峰值)
        Returns:
            bbox:        [B, 4]  (cx, cy, w, h) in search 像素坐标
            confidence:  [B]     综合分数
            response:    [B, H, W]  最终响应图 (含汉宁窗)
        """
        cls_map = head_out["cls_map"]          # [B, 1, H, W]
        reg_map = head_out["reg_map"]          # [B, 4, H, W]
        quality_map = head_out["quality_map"]  # [B, 1, H, W]

        B, _, H, W = cls_map.shape
        device = cls_map.device

        # cls + quality 联合分数
        score = torch.sigmoid(cls_map) * torch.sigmoid(quality_map)
        score = score.squeeze(1)               # [B, H, W]

        # 汉宁窗抑制 (远离中心的峰值降权)
        # 简化版: 静态汉宁窗。完整版应基于 last_bbox 位置。
        h_window = torch.hann_window(H, device=device)
        w_window = torch.hann_window(W, device=device)
        hann2d = h_window.unsqueeze(1) * w_window.unsqueeze(0)  # [H, W]
        hw = getattr(self, "hann_weight", 0.5)   # 默认 0.5 = 原行为; 诊断/消融可设 0 关掉中心先验
        score = score * hann2d.unsqueeze(0) * hw + score * (1.0 - hw)

        # 找全局最大值
        flat = score.view(B, -1)
        max_vals, max_idx = flat.max(dim=-1)   # [B]
        cy = (max_idx // W).float()
        cx = (max_idx % W).float()

        # 取 reg 在峰值位置的值: [l, t, r, b] (单位: 特征图像素)
        batch_idx = torch.arange(B, device=device)
        reg_at_peak = reg_map[batch_idx, :, cy.long(), cx.long()]  # [B, 4]
        l, t, r, b = reg_at_peak.unbind(dim=-1)

        # 转回 search 像素坐标系
        s = self.feat_stride
        cx_pix = (cx + 0.5) * s
        cy_pix = (cy + 0.5) * s
        x1 = cx_pix - l * s
        y1 = cy_pix - t * s
        x2 = cx_pix + r * s
        y2 = cy_pix + b * s

        bbox_w = (x2 - x1).clamp(min=1.0)
        bbox_h = (y2 - y1).clamp(min=1.0)
        bbox_cx = (x1 + x2) / 2
        bbox_cy = (y1 + y2) / 2
        bbox = torch.stack([bbox_cx, bbox_cy, bbox_w, bbox_h], dim=-1)

        return {
            "bbox": bbox,
            "confidence": max_vals,
            "response": score,
        }
