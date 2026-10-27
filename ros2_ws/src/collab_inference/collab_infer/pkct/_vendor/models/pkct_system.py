"""
PKCTSystem: 完整的渐进式知识协同跟踪系统

组装 Backbone + TrackingHead + ExpertPool + Router + KalmanFusion
+ ReIDFusion + GlobalTrackState

这是单架无人机上运行的完整跟踪 pipeline.

推理时间线 (一帧):
┌────────────────────────────────────────────────────────────┐
│ T = 0ms    Camera 捕获新一帧                                │
│ T = 1ms    根据上帧位置裁剪搜索区 + Backbone 提取特征         │
│ T = 5ms    跟踪头输出本地 bbox + 置信度                      │
│ T = 6ms    卡尔曼预测 + 本地 update + 质量评估                │
│ T = 6ms    输出第一版 bbox (本地结果)                        │
│            同时: Router 决策 → 发协同请求 + 编码本地碎片      │
│ T = 12ms   第一个远程视角增量到达 → 几何对齐 → 卡尔曼 update  │
│ T = 18ms   第二个增量 → 继续 update                          │
│ T = 25ms   第三个增量 / 超时 → 输出最终 bbox                  │
│ T = 26ms   GlobalTrackState 更新 (轨迹 + 模板缓存)            │
└────────────────────────────────────────────────────────────┘

与 PKCI 系统的对应关系:
  PKCI                       PKCT
  ------                     ------
  Backbone (cls)             MixFormerLite (one-stream tracker)
  ExpertPool (异构)           ExpertPool (同构, 自分化)
  Router                     TemporalRouter (3 层决策)
  ProgressiveFusion          ExtendedKalmanFusion + ReIDFusion
  forward_local_only         forward_local_only
  forward_with_fragments     forward_with_fragments
  generate_fragment          generate_fragment
  (无)                        GlobalTrackState (新增第四层)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import List, Optional

from .backbone import MixFormerLite
from .tracking_head import TrackingHead
from .expert import ExpertPool
from .router import TemporalRouter
from .temporal_memory import SelectiveTemporalMemory
from .kalman_fusion import ExtendedKalmanFusion, KalmanState
from .reid_fusion import ReIDFusion
from .global_state import GlobalTrackState, TrackStatus
from ..communication.tracking_fragment import (
    TrackingFragment, TrackingFragmentEncoder, TrackingFragmentBuffer,
)


class PKCTSystem(nn.Module):
    """
    单架无人机的完整 PKCT 跟踪系统.
    """

    def __init__(
        self,
        drone_id: int = 0,
        embed_dim: int = 192,
        depth: int = 4,
        num_heads: int = 6,
        feature_dim: int = 256,
        reid_dim: int = 128,
        num_experts: int = 3,
        expert_hidden: int = 256,   # 必须 = len(TemporalRouter.REGIMES) - 1 (v2: 3 种困境)
        template_size: int = 128,
        search_size: int = 256,
        feat_stride: int = 16,
        history_len: int = 30,
        backbone_type: str = "mixformer_lite",   # 或 "deit_tiny" (实验二)
    ):
        super().__init__()
        # 专精专家数必须与困境路由档位 (normal + N 困境) 对齐, 否则 assemble 的
        # regime_weights[:, 1:1+N] 会与困境标签错位.
        assert num_experts == len(TemporalRouter.REGIMES) - 1, (
            f"num_experts({num_experts}) 必须 = REGIMES-1"
            f"({len(TemporalRouter.REGIMES) - 1})"
        )
        self.drone_id = drone_id
        self.embed_dim = embed_dim
        self.feature_dim = feature_dim
        self.reid_dim = reid_dim
        self.search_size = search_size

        # ─── 单机跟踪层 ───────────────────────────────────────
        self.backbone_type = backbone_type
        if backbone_type == "deit_tiny":
            from .backbone_deit import DeiTTinyBackbone
            self.backbone = DeiTTinyBackbone(
                embed_dim=embed_dim, feature_dim=feature_dim,
                template_size=template_size, search_size=search_size,
                stride=feat_stride,
            )
        else:
            self.backbone = MixFormerLite(
                embed_dim=embed_dim, num_heads=num_heads, depth=depth,
                feature_dim=feature_dim,
                template_size=template_size, search_size=search_size,
                stride=feat_stride,
            )
        self.head = TrackingHead(
            in_dim=embed_dim, search_size=search_size,
            feat_stride=feat_stride,
        )
        # expert_hidden: 专家内部宽度. 默认 256 → 每专家 ~0.56M, 池合计 2.24M
        # (占整机 5.9M 的 38%). 消融显示 experts 单独只 +0.007 IoU, 但与时序记忆
        # 组合 +0.041(强超加性) —— experts-lite 实验就是调这个旋钮找性价比点.
        self.expert_pool = ExpertPool(
            num_experts=num_experts,
            embed_dim=embed_dim,
            feat_size=search_size // feat_stride,
            hidden_dim=expert_hidden,
            reid_dim=reid_dim,
        )

        # 共享 ReID head (身份核心枢纽; 输入是目标区域池化特征, 非全局池化)
        self.reid_head = nn.Sequential(
            nn.Linear(embed_dim, embed_dim),
            nn.ReLU(inplace=True),
            nn.Linear(embed_dim, reid_dim),
        )

        # ─── 时序记忆层 (Mamba 式选择性时序记忆) ───────────────
        self.temporal_memory = SelectiveTemporalMemory(
            in_dim=reid_dim, state_dim=128, cond_dim=embed_dim,
        )

        # ─── 协同决策层 ───────────────────────────────────────
        self.router = TemporalRouter(
            feature_dim=feature_dim, history_len=5,
        )
        self.kalman = ExtendedKalmanFusion()
        self.reid_fusion = ReIDFusion(reid_dim=reid_dim)

        # ─── 全局状态层 ───────────────────────────────────────
        self.global_state = GlobalTrackState(history_len=history_len)
        self.kalman_state: Optional[KalmanState] = None
        # 推理时缓存的模板 token (整段视频不变); reset() 失效
        self._cached_template_tokens: Optional[tuple] = None

        # ─── 通信缓冲 ───────────────────────────────────────
        self.fragment_buffer = TrackingFragmentBuffer(timeout_ms=20.0)

    # ════════════════════════════════════════════════════════
    # 工具
    # ════════════════════════════════════════════════════════

    def _target_pool(
        self, search_feat: torch.Tensor, bbox: torch.Tensor,
    ) -> torch.Tensor:
        """
        目标区域池化: 在 bbox 中心双线性采样 search_feat → [B, C].

        替代"search 全局池化"——航拍小目标若全局池化, 背景占主导, 身份不可靠;
        在目标中心采样得到目标自身的特征. (v1 用中心点采样, 大目标可换 roi_align.)
        """
        B, C = search_feat.shape[:2]
        # bbox 中心 (search 像素) → 归一化到 [-1, 1]
        cx = bbox[:, 0] / self.search_size * 2.0 - 1.0
        cy = bbox[:, 1] / self.search_size * 2.0 - 1.0
        grid = torch.stack([cx, cy], dim=-1).view(B, 1, 1, 2)   # [B,1,1,2]
        sampled = F.grid_sample(
            search_feat, grid, mode="bilinear",
            padding_mode="border", align_corners=False,
        )                                                        # [B, C, 1, 1]
        return sampled.view(B, C)

    # ════════════════════════════════════════════════════════
    # 训练接口 (batch-aware, 不经过 fragment 编码)
    # ════════════════════════════════════════════════════════

    def forward_train(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        use_temporal: bool = False,
    ) -> dict:
        """
        训练时基础前向: 不做协同, 输出本地跟踪 + 所有专家输出.

        use_temporal=True 时启用选择性时序记忆 (因果顺序: condition→head→reid→update),
        需调用方在片段开始 reset() 并按帧顺序调用 (Phase 时序训练 / 推理). 单帧
        独立 batch 训练用 use_temporal=False.
        """
        bb_out = self.backbone(template_img, search_img)
        # 时序记忆: 用上帧状态 FiLM 条件化 search 特征 (读)
        search_feat = bb_out["search_feature"]
        if use_temporal:
            search_feat = self.temporal_memory.condition(search_feat)
        head_out = self.head(search_feat)

        # 解码本地粗 bbox (用于送给专家做 refinement)
        with torch.no_grad():
            decoded = self.head.decode_bbox(head_out)
        coarse_bbox = decoded["bbox"]

        # 共享 reid: 目标区域池化 (非全局), 身份核心枢纽
        target_feat = self._target_pool(search_feat, coarse_bbox)
        shared_reid = F.normalize(self.reid_head(target_feat), dim=-1)

        # 时序记忆: 用当前帧目标观测选择性写入 (写, 由 quality 门控)
        if use_temporal:
            self.temporal_memory.update(shared_reid, decoded["confidence"])

        # 困境路由 + 专家软组装修正: corrected = anchor + δ_shared + Σ a_k·δ_k.
        # decoded_bbox 是稳定锚, corrected_bbox 是 MoE 修正后的输出 (P2 起监督它).
        regime = self.router.forward_regime(bb_out["feature_vec"])
        assembled = self.expert_pool.assemble(
            search_feat,
            bb_out["template_feature"],
            coarse_bbox,
            regime["regime_weights"],
        )

        return {
            "head_output": head_out,
            "decoded_bbox": coarse_bbox,
            "decoded_confidence": decoded["confidence"],
            "feature_vec": bb_out["feature_vec"],
            "search_feature": search_feat,
            "template_feature": bb_out["template_feature"],
            "shared_reid": shared_reid,
            "all_expert_outputs": assembled["specialists"],
            "regime_logits": regime["regime_logits"],
            "regime_weights": regime["regime_weights"],
            "corrected_bbox": assembled["corrected_bbox"],
            "corrected_reid": assembled["reid_embed"],
        }

    @torch.no_grad()
    def forward_local_features(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        use_template_cache: bool = False,
        use_experts: bool = True,
        use_temporal: bool = True,
    ) -> dict:
        """
        推理前向: backbone + 选择性时序记忆 + head + reid (+ 可选 MoE 修正).

        use_experts=True (默认): 跑困境路由 + 专家软组装, 输出 corrected_bbox.
        use_experts=False: 只出 anchor, 跳过专家 (单机基线评测 / 最快路径).
        use_temporal=True (默认): 用持久时序记忆 (因果: condition→head→reid→update);
          流式推理需逐帧调用 + 新视频 reset(). 单帧基线评测可设 False.
        use_template_cache=True: 复用缓存模板 token, 跳过模板 stem.
        """
        cached = None
        if use_template_cache:
            if self._cached_template_tokens is None:
                self._cached_template_tokens = self.backbone.encode_template_tokens(
                    template_img,
                )
            cached = self._cached_template_tokens

        bb_out = self.backbone(template_img, search_img, cached_template_tokens=cached)
        search_feat = bb_out["search_feature"]
        if use_temporal:
            search_feat = self.temporal_memory.condition(search_feat)   # 读
        head_out = self.head(search_feat)
        decoded = self.head.decode_bbox(head_out)

        # 目标区域池化 reid (非全局)
        target_feat = self._target_pool(search_feat, decoded["bbox"])
        shared_reid = F.normalize(self.reid_head(target_feat), dim=-1)
        if use_temporal:
            self.temporal_memory.update(shared_reid, decoded["confidence"])  # 写

        out = {
            "head_output": head_out,
            "decoded_bbox": decoded["bbox"],
            "decoded_confidence": decoded["confidence"],
            "shared_reid": shared_reid,
            "feature_vec": bb_out["feature_vec"],
            "search_feature": search_feat,
            "template_feature": bb_out["template_feature"],
        }

        if use_experts:
            regime = self.router.forward_regime(bb_out["feature_vec"])
            assembled = self.expert_pool.assemble(
                search_feat, bb_out["template_feature"],
                decoded["bbox"], regime["regime_weights"],
            )
            out["corrected_bbox"] = assembled["corrected_bbox"]
            out["corrected_reid"] = assembled["reid_embed"]
            out["regime_weights"] = regime["regime_weights"]

        return out

    def forward_with_remote_observations(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        remote_bboxes: List[torch.Tensor],
        remote_confidences: List[float],
        remote_reids: Optional[List[torch.Tensor]] = None,
        view_angle_factors: Optional[List[float]] = None,
        local_out: Optional[dict] = None,
    ) -> dict:
        """
        训练 Phase 4: 直接接受远程 bbox+ReID, 不经过 fragment 编码,
        保留 batch 维以便梯度回传.

        类比 PKCI 的 forward_with_expert_logits.

        Args:
            remote_bboxes:        list of [B, 4]
            remote_confidences:   list of float (整 batch 共用一个 confidence,
                                  类比 PKCI 中 fragment_confidence_list)
            remote_reids:         list of [B, reid_dim] 或 None
            local_out:            预计算的 forward_train 输出 (复用以避免重复前向).
        """
        if local_out is None:
            local_out = self.forward_train(template_img, search_img)
        B = template_img.shape[0]
        device = template_img.device

        local_bbox = local_out["decoded_bbox"]                # [B, 4]
        local_conf = local_out["decoded_confidence"]          # [B]
        local_reid = local_out["shared_reid"]                 # [B, reid_dim]

        # 卡尔曼融合 (在 batch 维上独立做)
        if remote_bboxes:
            fused_bbox_list = []
            for b in range(B):
                lcoal_b = local_bbox[b]
                # 训练时无时序状态, 用 local 初始化
                state = KalmanState.from_bbox(lcoal_b)
                state = self.kalman.update_single(
                    state, lcoal_b, float(local_conf[b].item()),
                )
                vaf = view_angle_factors or [1.0] * len(remote_bboxes)
                for k, rb in enumerate(remote_bboxes):
                    state = self.kalman.update_single(
                        state, rb[b], remote_confidences[k], vaf[k],
                    )
                fused_bbox_list.append(state.x[:4])
            fused_bbox = torch.stack(fused_bbox_list, dim=0)
        else:
            fused_bbox = local_bbox

        # ReID 融合
        if remote_reids:
            fused_reid_list = []
            for b in range(B):
                fused_b = self.reid_fusion.fuse(
                    local_reid[b], float(local_conf[b].item()),
                    [r[b] for r in remote_reids],
                    remote_confidences,
                    view_angle_factors,
                )
                fused_reid_list.append(fused_b)
            fused_reid = torch.stack(fused_reid_list, dim=0)
        else:
            fused_reid = local_reid

        # 路由器前向 (训练时也要参与, 计算 router loss)
        router_out = self.router(
            local_out["feature_vec"], local_conf,
        )

        return {
            **local_out,
            "fused_bbox": fused_bbox,
            "fused_reid": fused_reid,
            "router_output": router_out,
        }

    def forward_with_fragments(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        received_fragments: List[TrackingFragment],
    ) -> dict:
        """
        通信仿真接口: 输入是已收到的碎片列表 (每个碎片对应 batch=1).

        训练 Phase 4 + 通信 Dropout 的桥梁: comm_dropout 处理 fragment 列表,
        然后这里把碎片解码为 batch tensor 再做融合.
        """
        local_out = self.forward_train(template_img, search_img)
        B = template_img.shape[0]
        device = template_img.device

        if not received_fragments:
            return {
                **local_out,
                "fused_bbox": local_out["decoded_bbox"],
                "fused_reid": local_out["shared_reid"],
                "num_fragments": 0,
            }

        remote_bboxes = []
        remote_confidences = []
        remote_reids = []
        for frag in received_fragments:
            if frag.bbox is None:
                continue
            # 复制到 batch 维 (单帧推理时 B=1; 训练时碎片是单条记录)
            bbox_b = frag.bbox.unsqueeze(0).expand(B, -1).to(device)
            remote_bboxes.append(bbox_b)
            remote_confidences.append(frag.confidence)
            if frag.reid_embed is not None:
                reid_b = frag.reid_embed.unsqueeze(0).expand(B, -1).to(device)
                remote_reids.append(reid_b)
            else:
                # 用零向量占位
                remote_reids.append(torch.zeros(B, self.reid_dim, device=device))

        fused_out = self.forward_with_remote_observations(
            template_img, search_img,
            remote_bboxes, remote_confidences, remote_reids,
        )
        fused_out["num_fragments"] = len(received_fragments)
        return fused_out

    # ════════════════════════════════════════════════════════
    # 推理接口 (单帧, B=1, 维护时序状态)
    # ════════════════════════════════════════════════════════

    @torch.no_grad()
    def forward_local_only(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        use_experts: bool = True,
        use_temporal: bool = True,
    ) -> dict:
        """
        纯本地推理 (通信不可用时使用). 维护 GlobalTrackState 但不做协同.
        use_experts=False 时退化为 anchor-only 的最快路径 (跳过 MoE).
        """
        local_out = self.forward_local_features(
            template_img, search_img, use_template_cache=True,
            use_experts=use_experts, use_temporal=use_temporal,
        )

        # 卡尔曼: predict + 单观测 update
        if use_experts and "corrected_bbox" in local_out:
            bbox = local_out["corrected_bbox"][0]
            reid = local_out["corrected_reid"][0]
        else:
            bbox = local_out["decoded_bbox"][0]
            reid = local_out["shared_reid"][0]
        conf = local_out["decoded_confidence"][0].item()

        if self.kalman_state is None or not self.kalman_state.initialized:
            self.kalman_state = KalmanState.from_bbox(bbox)
        else:
            self.kalman_state = self.kalman.predict(self.kalman_state)
        self.kalman_state = self.kalman.update_single(
            self.kalman_state, bbox, conf, 1.0,
        )
        smoothed_bbox = self.kalman_state.x[:4]

        return {
            "bbox": smoothed_bbox,
            "confidence": conf,
            "reid_embed": reid,
            "path": "local_only",
            "head_output": local_out["head_output"],
        }

    @torch.no_grad()
    def step(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        frame_id: int = 0,
        timestamp: float = 0.0,
        received_fragments: Optional[List[TrackingFragment]] = None,
        use_experts: bool = True,
        use_temporal: bool = True,
    ) -> dict:
        """
        单帧推理入口 (B=1, 完整流程).

        路径:
          1. backbone + head → 本地 bbox
          2. kalman predict + local update
          3. (如果有 fragments) 路由判断 → reid 一致性检查 → kalman 多观测 update
          4. 更新 global_state
          5. 输出最终 bbox + status
        """
        # Step 1: 本地特征 + 跟踪头 (use_experts 决定是否叠加 MoE 修正)
        local_out = self.forward_local_features(
            template_img, search_img, use_template_cache=True,
            use_experts=use_experts, use_temporal=use_temporal,
        )
        if use_experts and "corrected_bbox" in local_out:
            local_bbox = local_out["corrected_bbox"][0]
            local_reid = local_out["corrected_reid"][0]
        else:
            local_bbox = local_out["decoded_bbox"][0]
            local_reid = local_out["shared_reid"][0]
        local_conf = float(local_out["decoded_confidence"][0].item())

        # Step 2: 卡尔曼 predict + local update
        if self.kalman_state is None or not self.kalman_state.initialized:
            self.kalman_state = KalmanState.from_bbox(local_bbox)
        else:
            self.kalman_state = self.kalman.predict(self.kalman_state)
        self.kalman_state = self.kalman.update_single(
            self.kalman_state, local_bbox, local_conf, 1.0,
        )

        fused_bbox = self.kalman_state.x[:4].clone()
        fused_reid = local_reid

        # Step 3: 远程观测渐进融合
        num_fused = 0
        if received_fragments:
            # 收集有 reid 的 fragments + 它们在原列表的索引
            reid_fragments = [(k, f) for k, f in enumerate(received_fragments)
                              if f.reid_embed is not None]
            reid_embeds = [f.reid_embed.to(local_reid.device)
                           for _, f in reid_fragments]
            consistency = self.reid_fusion.consistency_check(
                local_reid, reid_embeds, threshold=0.5,
            )
            # valid_set 索引的是原 received_fragments 列表
            valid_indices_in_full = {
                reid_fragments[i][0] for i in consistency["valid_indices"]
            }

            valid_remote_reids = []
            valid_remote_confs = []

            for k, frag in enumerate(received_fragments):
                if frag.bbox is None:
                    continue
                # 有 reid 但一致性失败的跳过; 没有 reid 的不做一致性判断 (由 bbox 主导)
                if frag.reid_embed is not None and k not in valid_indices_in_full:
                    continue
                self.kalman_state = self.kalman.progressive_update(
                    self.kalman_state,
                    frag.bbox.to(local_bbox.device),
                    frag.confidence,
                    1.0,
                )
                num_fused += 1

                if frag.reid_embed is not None:
                    valid_remote_reids.append(frag.reid_embed.to(local_reid.device))
                    valid_remote_confs.append(frag.confidence)

            fused_bbox = self.kalman_state.x[:4].clone()
            if valid_remote_reids:
                fused_reid = self.reid_fusion.fuse(
                    local_reid, local_conf,
                    valid_remote_reids, valid_remote_confs,
                )

        # Step 4: 更新全局状态
        status = self.global_state.update(
            frame_id=frame_id, timestamp=timestamp,
            bbox=fused_bbox, confidence=local_conf,
            reid_embed=fused_reid, fused=(num_fused > 0),
        )

        return {
            "bbox": fused_bbox,
            "confidence": local_conf,
            "reid_embed": fused_reid,
            "status": status,
            "num_fragments_used": num_fused,
            "path": "collab" if num_fused > 0 else "local_only",
            "kalman_state": self.kalman_state,
            "head_output": local_out["head_output"],
        }

    # ════════════════════════════════════════════════════════
    # 通信接口
    # ════════════════════════════════════════════════════════

    @torch.no_grad()
    def generate_fragment(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        bandwidth_level: str = "medium",
        timestamp: float = 0.0,
        expert_idx: int = 0,
    ) -> TrackingFragment:
        """
        为远程请求生成 TrackingFragment (B=1).
        被其他无人机调用.
        """
        # use_temporal=False: 生成碎片不应扰动本机自己跟踪的流式时序状态
        local_out = self.forward_local_features(
            template_img, search_img, use_template_cache=True,
            use_experts=False, use_temporal=False,
        )
        bbox = local_out["decoded_bbox"][0]
        conf = float(local_out["decoded_confidence"][0].item())
        reid = local_out["shared_reid"][0]
        # 从本地 kalman state 拿速度 (如果有)
        if self.kalman_state is not None and self.kalman_state.initialized:
            velocity = self.kalman_state.x[4:8]
        else:
            velocity = torch.zeros(4)

        return TrackingFragmentEncoder.encode(
            drone_id=self.drone_id,
            expert_id=expert_idx,
            bbox=bbox.cpu(),
            velocity=velocity.cpu(),
            reid=reid.cpu(),
            confidence=conf,
            bandwidth_level=bandwidth_level,
            timestamp=timestamp,
        )

    # ════════════════════════════════════════════════════════
    # 状态管理
    # ════════════════════════════════════════════════════════

    def reset(self):
        """新一段视频开始时清空时序状态."""
        self.kalman_state = None
        self.global_state = GlobalTrackState(
            history_len=self.global_state.history_len,
        )
        self.fragment_buffer.reset(0.0)
        self._cached_template_tokens = None
        self.temporal_memory.reset()


class PKCTFleet:
    """
    多无人机机群模拟器 (训练 + 测试用).

    与 PKCIFleet 接口对应, 但每架无人机维护独立的时序状态.
    """

    def __init__(
        self,
        num_drones: int = 5,
        embed_dim: int = 192,
        feature_dim: int = 256,
        reid_dim: int = 128,
        num_experts_per_drone: int = 3,
        template_size: int = 128,
        search_size: int = 256,
    ):
        self.num_drones = num_drones
        self.drones: List[PKCTSystem] = []
        for i in range(num_drones):
            drone = PKCTSystem(
                drone_id=i,
                embed_dim=embed_dim,
                feature_dim=feature_dim,
                reid_dim=reid_dim,
                num_experts=num_experts_per_drone,
                template_size=template_size,
                search_size=search_size,
            )
            self.drones.append(drone)

    def to(self, device: torch.device) -> "PKCTFleet":
        for d in self.drones:
            d.to(device)
        return self

    def reset_all(self):
        for d in self.drones:
            d.reset()

    def sync_from_drone0(self):
        """把 drone0 的权重复制到其余同构无人机.

        机群是同构的, 训练只更新 drone0 (N× 更省显存、样本效率更高), 推理/仿真
        前用本方法让所有无人机共享同一套权重 — 这才是同构机群的预期状态.
        """
        if self.num_drones <= 1:
            return
        state = self.drones[0].state_dict()
        for d in self.drones[1:]:
            d.load_state_dict(state)

    def simulate_collaborative_step(
        self,
        template_imgs: List[torch.Tensor],
        search_imgs: List[torch.Tensor],
        requesting_drone: int = 0,
        bandwidth_level: str = "medium",
        available_drones: Optional[List[int]] = None,
        frame_id: int = 0,
        timestamp: float = 0.0,
    ) -> dict:
        """
        模拟一帧的协同跟踪.

        Args:
            template_imgs:   每架无人机的模板 (各自视角)
            search_imgs:     每架无人机的搜索区域 (各自视角)
            requesting_drone: 发起协同的无人机 (我们关心其输出)
            available_drones: 可用邻居列表 (None = 全部)
        """
        if available_drones is None:
            available_drones = list(range(self.num_drones))

        # 收集邻居碎片
        fragments = []
        for did in available_drones:
            if did == requesting_drone:
                continue
            drone = self.drones[did]
            frag = drone.generate_fragment(
                template_imgs[did], search_imgs[did],
                bandwidth_level=bandwidth_level, timestamp=timestamp,
            )
            fragments.append(frag)

        # 请求方做协同推理
        requester = self.drones[requesting_drone]
        result = requester.step(
            template_imgs[requesting_drone],
            search_imgs[requesting_drone],
            frame_id=frame_id, timestamp=timestamp,
            received_fragments=fragments,
        )
        result["num_fragments_received"] = len(fragments)
        result["available_drones"] = len(available_drones)
        return result
