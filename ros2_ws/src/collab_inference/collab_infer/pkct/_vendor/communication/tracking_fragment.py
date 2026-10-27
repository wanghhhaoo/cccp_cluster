"""
TrackingFragment: 跟踪场景的知识增量

对应 PKCI 的 KnowledgeFragment, 但承载的不再是 logits Top-K,
而是结构化的 bbox + ReID + motion 信息.

碎片大小连续可调 (12 字节 ~ 300+ 字节):
  - 12B:   仅 header (目标丢失通知 / 心跳)
  - 40B:   bbox + 16-d PCA ReID (轻量视角校准)
  - 100B:  bbox + 速度 + 32-d ReID (一般 re-detection)
  - 300B+: bbox + 速度 + 协方差 + 128-d ReID (完整搜索)

Header 字节布局 (12 B):
  drone_id        uint8     1B
  expert_id       uint8     1B
  timestamp       uint32    4B    (ms 精度)
  frame_id        uint32    4B    (跟踪帧序号)
  flags           uint8     1B    (位标志)
  confidence      float16   2B    (但 struct 不直接支持 f16, 用 uint16 reinterpret)
"""

import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import torch


# ─── 位标志 ────────────────────────────────────────────────
FLAG_HAS_BBOX     = 0x01
FLAG_HAS_VELOCITY = 0x02
FLAG_HAS_COV      = 0x04
FLAG_HAS_REID     = 0x08
FLAG_HAS_MOTION   = 0x10
FLAG_LOST         = 0x20  # 通知邻居本机已丢失目标


@dataclass
class TrackingFragment:
    """
    跟踪知识增量.

    设计原则: 信息量随碎片大小平滑衰减.
    """
    drone_id: int
    expert_id: int
    timestamp: float
    frame_id: int
    confidence: float
    flags: int = 0

    # bbox payload (可选)
    bbox: Optional[torch.Tensor] = None             # [4] (cx, cy, w, h) float16
    velocity: Optional[torch.Tensor] = None         # [4] (vcx, vcy, vw, vh)
    cov_diag: Optional[torch.Tensor] = None         # [4] 对角线协方差

    # ReID payload (可选, 维度可调 16/32/64/128)
    reid_embed: Optional[torch.Tensor] = None       # [reid_dim]
    reid_dim: int = 0

    # Motion prediction payload (可选)
    predicted_bbox: Optional[torch.Tensor] = None   # [4]
    prediction_steps: int = 0

    def size_bytes(self) -> int:
        """估算字节大小 (与 serialize() 输出长度一致)."""
        n = 12  # header
        if self.flags & FLAG_HAS_BBOX:
            n += 4 * 2          # bbox float16
        if self.flags & FLAG_HAS_VELOCITY:
            n += 4 * 2
        if self.flags & FLAG_HAS_COV:
            n += 4 * 2
        if self.flags & FLAG_HAS_REID:
            n += 1 + self.reid_dim * 2  # 1 byte for dim + payload
        if self.flags & FLAG_HAS_MOTION:
            n += 4 * 2 + 1
        return n

    def has_payload(self, flag: int) -> bool:
        return bool(self.flags & flag)


class TrackingFragmentEncoder:
    """
    碎片编码器: 根据带宽等级和请求类型选择 payload 内容.

    带宽等级 → bbox 精度等级 + ReID 维度的预设映射.
    """

    # 带宽 → bbox 等级
    BBOX_LEVEL = {
        "full":    "full",      # bbox + velocity + cov_diag (24B)
        "high":    "standard",  # bbox + velocity (16B)
        "medium":  "standard",
        "low":     "compact",   # bbox only (8B)
        "minimal": "compact",
        "crisis":  "minimal",   # bbox 4 字节量化 (4B) — 此处简化为不发 bbox
    }

    # 带宽 → ReID 维度
    REID_DIM = {
        "full":    128,
        "high":    64,
        "medium":  32,
        "low":     16,
        "minimal": 0,    # 不发 reid
        "crisis":  0,
    }

    @staticmethod
    def encode(
        drone_id: int,
        expert_id: int,
        bbox: Optional[torch.Tensor],
        velocity: Optional[torch.Tensor],
        reid: Optional[torch.Tensor],
        confidence: float,
        bandwidth_level: str = "medium",
        timestamp: float = 0.0,
        frame_id: int = 0,
        cov_diag: Optional[torch.Tensor] = None,
        predicted_bbox: Optional[torch.Tensor] = None,
        prediction_steps: int = 0,
        request_type: str = "bbox_calib",
    ) -> TrackingFragment:
        """
        编码一个碎片.

        Args:
            request_type: 影响 payload 选择:
                - "bbox_calib": 仅 bbox + 精简 reid
                - "reid_verify": bbox + 标准 reid
                - "full_search": 全部 payload
                - "lost_notify": 仅 header + FLAG_LOST
        """
        flags = 0
        bbox_level = TrackingFragmentEncoder.BBOX_LEVEL.get(bandwidth_level, "standard")
        reid_target_dim = TrackingFragmentEncoder.REID_DIM.get(bandwidth_level, 32)

        # request_type 进一步约束
        if request_type == "lost_notify":
            return TrackingFragment(
                drone_id=drone_id, expert_id=expert_id,
                timestamp=timestamp, frame_id=frame_id,
                confidence=confidence, flags=FLAG_LOST,
            )
        if request_type == "bbox_calib" and reid_target_dim > 32:
            reid_target_dim = 32  # 视角校准只要轻量 reid
        if request_type == "full_search":
            bbox_level = "full"
            reid_target_dim = max(reid_target_dim, 64)

        # ── bbox payload ─────────────────────────────────
        bbox_out = None
        velocity_out = None
        cov_out = None
        if bbox is not None and bbox_level != "none":
            flags |= FLAG_HAS_BBOX
            bbox_out = bbox.detach().cpu().to(torch.float16)
            if bbox_level in ("standard", "full") and velocity is not None:
                flags |= FLAG_HAS_VELOCITY
                velocity_out = velocity.detach().cpu().to(torch.float16)
            if bbox_level == "full" and cov_diag is not None:
                flags |= FLAG_HAS_COV
                cov_out = cov_diag.detach().cpu().to(torch.float16)

        # ── ReID payload (含降维) ────────────────────────
        reid_out = None
        reid_dim = 0
        if reid is not None and reid_target_dim > 0:
            flags |= FLAG_HAS_REID
            full_reid = reid.detach().cpu().to(torch.float16)
            if full_reid.shape[-1] > reid_target_dim:
                # 简化的 PCA 降维: 取前 K 维 (实际部署应用学到的投影)
                reid_out = full_reid[:reid_target_dim]
            else:
                reid_out = full_reid
            reid_dim = reid_out.shape[-1]

        # ── Motion payload ───────────────────────────────
        if predicted_bbox is not None and prediction_steps > 0:
            flags |= FLAG_HAS_MOTION

        return TrackingFragment(
            drone_id=drone_id, expert_id=expert_id,
            timestamp=timestamp, frame_id=frame_id,
            confidence=confidence, flags=flags,
            bbox=bbox_out, velocity=velocity_out, cov_diag=cov_out,
            reid_embed=reid_out, reid_dim=reid_dim,
            predicted_bbox=(predicted_bbox.detach().cpu().to(torch.float16)
                            if predicted_bbox is not None else None),
            prediction_steps=prediction_steps,
        )

    @staticmethod
    def serialize(fragment: TrackingFragment) -> bytes:
        """字节流序列化 (用于网络传输)."""
        # confidence: clamp [0,1] → uint16
        conf_u16 = int(np.clip(fragment.confidence, 0.0, 1.0) * 65535)

        header = struct.pack(
            "<BBIIBH",
            fragment.drone_id & 0xFF,
            fragment.expert_id & 0xFF,
            int(fragment.timestamp * 1000) & 0xFFFFFFFF,
            fragment.frame_id & 0xFFFFFFFF,
            fragment.flags & 0xFF,
            conf_u16,
        )
        payload = b""
        if fragment.flags & FLAG_HAS_BBOX and fragment.bbox is not None:
            payload += fragment.bbox.numpy().astype(np.float16).tobytes()
        if fragment.flags & FLAG_HAS_VELOCITY and fragment.velocity is not None:
            payload += fragment.velocity.numpy().astype(np.float16).tobytes()
        if fragment.flags & FLAG_HAS_COV and fragment.cov_diag is not None:
            payload += fragment.cov_diag.numpy().astype(np.float16).tobytes()
        if fragment.flags & FLAG_HAS_REID and fragment.reid_embed is not None:
            payload += struct.pack("<B", fragment.reid_dim & 0xFF)
            payload += fragment.reid_embed.numpy().astype(np.float16).tobytes()
        if fragment.flags & FLAG_HAS_MOTION and fragment.predicted_bbox is not None:
            payload += fragment.predicted_bbox.numpy().astype(np.float16).tobytes()
            payload += struct.pack("<B", fragment.prediction_steps & 0xFF)
        return header + payload

    @staticmethod
    def deserialize(buf: bytes) -> TrackingFragment:
        """字节流反序列化."""
        header = struct.unpack_from("<BBIIBH", buf, 0)
        drone_id, expert_id, ts_ms, frame_id, flags, conf_u16 = header
        offset = struct.calcsize("<BBIIBH")
        confidence = conf_u16 / 65535.0

        frag = TrackingFragment(
            drone_id=drone_id, expert_id=expert_id,
            timestamp=ts_ms / 1000.0, frame_id=frame_id,
            confidence=confidence, flags=flags,
        )

        if flags & FLAG_HAS_BBOX:
            frag.bbox = torch.from_numpy(
                np.frombuffer(buf, dtype=np.float16, count=4, offset=offset).copy()
            )
            offset += 4 * 2
        if flags & FLAG_HAS_VELOCITY:
            frag.velocity = torch.from_numpy(
                np.frombuffer(buf, dtype=np.float16, count=4, offset=offset).copy()
            )
            offset += 4 * 2
        if flags & FLAG_HAS_COV:
            frag.cov_diag = torch.from_numpy(
                np.frombuffer(buf, dtype=np.float16, count=4, offset=offset).copy()
            )
            offset += 4 * 2
        if flags & FLAG_HAS_REID:
            (dim,) = struct.unpack_from("<B", buf, offset)
            offset += 1
            frag.reid_dim = dim
            frag.reid_embed = torch.from_numpy(
                np.frombuffer(buf, dtype=np.float16, count=dim, offset=offset).copy()
            )
            offset += dim * 2
        if flags & FLAG_HAS_MOTION:
            frag.predicted_bbox = torch.from_numpy(
                np.frombuffer(buf, dtype=np.float16, count=4, offset=offset).copy()
            )
            offset += 4 * 2
            (steps,) = struct.unpack_from("<B", buf, offset)
            frag.prediction_steps = steps
            offset += 1
        return frag


class TrackingFragmentBuffer:
    """
    碎片缓冲区: 收集一帧内到达的所有碎片.
    支持无序到达, 实现渐进式融合.
    """

    def __init__(self, timeout_ms: float = 20.0):
        self.timeout_ms = timeout_ms
        self.fragments: List[TrackingFragment] = []
        self.start_time: Optional[float] = None

    def reset(self, timestamp: float):
        self.fragments = []
        self.start_time = timestamp

    def add(self, fragment: TrackingFragment) -> bool:
        """添加碎片. Returns: True if accepted, False if timeout."""
        if self.start_time is None:
            self.start_time = fragment.timestamp
        elapsed_ms = (fragment.timestamp - self.start_time) * 1000.0
        if elapsed_ms > self.timeout_ms:
            return False
        self.fragments.append(fragment)
        return True

    def get_all(self) -> List[TrackingFragment]:
        return self.fragments.copy()

    def get_with_bbox(self) -> List[TrackingFragment]:
        return [f for f in self.fragments if f.has_payload(FLAG_HAS_BBOX)]

    def get_with_reid(self) -> List[TrackingFragment]:
        return [f for f in self.fragments if f.has_payload(FLAG_HAS_REID)]

    def __len__(self) -> int:
        return len(self.fragments)
