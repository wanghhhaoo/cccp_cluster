"""
GlobalTrackState: 全局轨迹状态管理

PKCT 相对 PKCI 新增的"第四层" — 跨帧维护目标的全局状态:
  - 轨迹历史 (最近 N 帧的 bbox + confidence)
  - 模板缓存 (初始模板 + 动态高置信度模板)
  - 运动模型 (速度估计、加速度估计)
  - 丢失状态机 (tracking / drift_warning / lost_short / lost_long)
  - ReID 模板库 (多个不同时刻的高质量 embedding)

丢失恢复机制:
  1. 连续 K 帧 confidence 低 → lost_short → 扩大搜索 + 启用 ReID 匹配
  2. 继续丢失 → lost_long → 请求所有邻居全图搜索
  3. ReID 匹配恢复后回到 tracking 状态
"""

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional, Tuple

import torch


class TrackStatus(Enum):
    """跟踪状态机."""
    TRACKING = "tracking"             # 正常跟踪
    DRIFT_WARNING = "drift_warning"   # 怀疑漂移 (低置信度但还没丢)
    LOST_SHORT = "lost_short"         # 短时丢失 (扩大搜索)
    LOST_LONG = "lost_long"           # 长时丢失 (全图协同搜索)


@dataclass
class TrackFrame:
    """单帧轨迹记录."""
    frame_id: int
    timestamp: float
    bbox: torch.Tensor             # [4] world / drone-local 坐标系
    confidence: float
    reid_embed: Optional[torch.Tensor] = None
    fused_with_collab: bool = False


class GlobalTrackState:
    """
    单架无人机维护的目标全局状态.

    Args:
        history_len:    保留多少帧轨迹历史 (用于运动估计 + 漂移诊断)
        template_max:   ReID 模板库容量
        drift_threshold:        confidence 低于此进入 drift_warning
        lost_short_threshold:   连续多少帧 confidence 低进入 lost_short
        lost_long_threshold:    连续多少帧未匹配到目标进入 lost_long
    """

    def __init__(
        self,
        history_len: int = 30,
        template_max: int = 5,
        drift_threshold: float = 0.4,
        lost_short_threshold: int = 5,
        lost_long_threshold: int = 30,
        template_update_threshold: float = 0.8,
    ):
        self.history_len = history_len
        self.template_max = template_max
        self.drift_threshold = drift_threshold
        self.lost_short_threshold = lost_short_threshold
        self.lost_long_threshold = lost_long_threshold
        self.template_update_threshold = template_update_threshold

        # 历史
        self.history: Deque[TrackFrame] = deque(maxlen=history_len)

        # 初始 + 动态模板 (Tensor 列表 + 对应 ReID embedding)
        self.template_imgs: List[torch.Tensor] = []
        self.template_reids: List[torch.Tensor] = []

        # 状态机
        self.status: TrackStatus = TrackStatus.TRACKING
        self.consecutive_low_conf: int = 0
        self.consecutive_no_match: int = 0

        # 运动模型 (简化: 用最近 5 帧线性回归)
        self.velocity: Optional[torch.Tensor] = None  # [4] (vcx, vcy, vw, vh)

    # ───── 模板管理 ─────────────────────────────────────────

    def init_template(self, template_img: torch.Tensor, reid_embed: torch.Tensor):
        """首帧调用: 注册初始模板 + 初始 ReID."""
        self.template_imgs.append(template_img.detach().cpu())
        self.template_reids.append(reid_embed.detach())

    def maybe_update_template(
        self,
        template_img: torch.Tensor,
        reid_embed: torch.Tensor,
        confidence: float,
    ) -> bool:
        """高置信度时更新模板库 (FIFO 但永远保留 index 0 的初始模板)."""
        if confidence < self.template_update_threshold:
            return False
        if len(self.template_imgs) >= self.template_max:
            # 删掉除初始外最老的
            self.template_imgs.pop(1)
            self.template_reids.pop(1)
        self.template_imgs.append(template_img.detach().cpu())
        self.template_reids.append(reid_embed.detach())
        return True

    def get_initial_template(self) -> Optional[torch.Tensor]:
        return self.template_imgs[0] if self.template_imgs else None

    # ───── 帧记录 + 状态机 ───────────────────────────────────

    def update(
        self,
        frame_id: int,
        timestamp: float,
        bbox: torch.Tensor,
        confidence: float,
        reid_embed: Optional[torch.Tensor] = None,
        fused: bool = False,
    ) -> TrackStatus:
        """
        每帧调用: 记录历史 + 更新状态机.

        Returns: 新的 TrackStatus
        """
        self.history.append(TrackFrame(
            frame_id=frame_id, timestamp=timestamp,
            bbox=bbox.detach().cpu(), confidence=confidence,
            reid_embed=reid_embed.detach().cpu() if reid_embed is not None else None,
            fused_with_collab=fused,
        ))

        # 更新计数
        if confidence < self.drift_threshold:
            self.consecutive_low_conf += 1
        else:
            self.consecutive_low_conf = 0

        # 状态转移
        if self.consecutive_low_conf == 0:
            self.status = TrackStatus.TRACKING
        elif self.consecutive_low_conf < self.lost_short_threshold:
            self.status = TrackStatus.DRIFT_WARNING
        elif self.consecutive_low_conf < self.lost_long_threshold:
            self.status = TrackStatus.LOST_SHORT
        else:
            self.status = TrackStatus.LOST_LONG

        # 更新运动估计
        self._update_velocity()

        return self.status

    def mark_recovered(self):
        """ReID 匹配成功恢复跟踪."""
        self.status = TrackStatus.TRACKING
        self.consecutive_low_conf = 0
        self.consecutive_no_match = 0

    def mark_no_match(self):
        """lost_short/lost_long 期间未找到目标."""
        self.consecutive_no_match += 1

    # ───── 运动估计 ───────────────────────────────────────

    def _update_velocity(self):
        """用最近 5 帧线性差分估计速度."""
        if len(self.history) < 2:
            self.velocity = None
            return
        recent = list(self.history)[-5:]
        if len(recent) < 2:
            return
        dt_total = recent[-1].timestamp - recent[0].timestamp
        if dt_total <= 0:
            return
        delta = recent[-1].bbox - recent[0].bbox
        self.velocity = delta / dt_total

    def predict_next_bbox(self, dt: float) -> Optional[torch.Tensor]:
        """根据当前速度推断下一帧 bbox (协同失联时的 fallback)."""
        if not self.history or self.velocity is None:
            return None
        last_bbox = self.history[-1].bbox
        return last_bbox + self.velocity * dt

    # ───── 查询接口 ───────────────────────────────────────

    def get_recent_confidences(self, n: int = 5) -> List[float]:
        return [f.confidence for f in list(self.history)[-n:]]

    def get_last_bbox(self) -> Optional[torch.Tensor]:
        return self.history[-1].bbox if self.history else None

    def is_lost(self) -> bool:
        return self.status in (TrackStatus.LOST_SHORT, TrackStatus.LOST_LONG)

    def needs_full_search(self) -> bool:
        return self.status == TrackStatus.LOST_LONG

    def to_summary(self) -> dict:
        return {
            "status": self.status.value,
            "history_len": len(self.history),
            "consecutive_low_conf": self.consecutive_low_conf,
            "consecutive_no_match": self.consecutive_no_match,
            "templates_cached": len(self.template_imgs),
            "velocity": self.velocity.tolist() if self.velocity is not None else None,
        }
