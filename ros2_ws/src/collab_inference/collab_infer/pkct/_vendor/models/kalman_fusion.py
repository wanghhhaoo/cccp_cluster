"""
KalmanFusion: 多视角扩展卡尔曼融合

与 PKCI 贝叶斯融合最大的不同:
  - PKCI: 概率分布的对数空间加权平均 (logits 域)
  - PKCT: 几何状态的多观测卡尔曼更新 (bbox + velocity 域)

保留的核心数学性质: 顺序无关 (commutative)
  独立观测的卡尔曼更新满足: x_{A→B} = x_{B→A}
  这是渐进式融合 "碎片可任意顺序到达" 的数学正当性.

状态向量 (8 维):
  x = [cx, cy, w, h, vx, vy, vw, vh]^T
  bbox 中心、宽高、以及对应速度.

观测模型:
  z_k = [cx, cy, w, h]_k    (观测的是 bbox, 不直接观测速度)
  z_k = H * x + n_k,  H = [I_4 | 0_4]
  R_k 由该增量的 confidence 和视角几何关系决定:
    - confidence 越高, R_k 越小, 权重越大
    - 视角越正面 (与请求方夹角小), R_k 越小

数学:
  predict:  x_{t|t-1} = F * x_{t-1|t-1}
            P_{t|t-1} = F * P_{t-1|t-1} * F^T + Q

  update (each obs k):
            S_k = H * P * H^T + R_k
            K_k = P * H^T * S_k^{-1}
            x   = x + K_k * (z_k - H * x)
            P   = (I - K_k * H) * P
"""

import torch
import torch.nn as nn
from typing import List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class KalmanState:
    """
    单个目标的卡尔曼状态.
    每架无人机各自维护自己的 (因为坐标系是本机坐标系).
    """
    x: torch.Tensor  # [8] 状态向量 [cx, cy, w, h, vx, vy, vw, vh]
    P: torch.Tensor  # [8, 8] 协方差矩阵
    initialized: bool = False
    last_update_time: float = 0.0

    @classmethod
    def from_bbox(cls, bbox: torch.Tensor, init_var: float = 100.0) -> "KalmanState":
        """从首帧 bbox 初始化."""
        device = bbox.device
        x = torch.zeros(8, device=device)
        x[:4] = bbox  # cx, cy, w, h
        # 速度初始为 0
        P = torch.eye(8, device=device) * init_var
        return cls(x=x, P=P, initialized=True)


class ExtendedKalmanFusion(nn.Module):
    """
    扩展卡尔曼多视角融合模块.

    与 PKCI ProgressiveFusion 的接口对应关系:
      forward()             ←→ 一次性融合所有观测 (训练时 batch 友好)
      progressive_update()  ←→ 增量融合 (运行时, 每碎片到达就调一次)
    """

    def __init__(
        self,
        process_noise: float = 1.0,
        base_obs_noise: float = 25.0,
        dt: float = 1.0 / 30.0,
        learnable_noise: bool = True,
    ):
        super().__init__()

        # 状态转移矩阵 F (恒速模型)
        F = torch.eye(8)
        F[0, 4] = dt   # cx += vx * dt
        F[1, 5] = dt   # cy += vy * dt
        F[2, 6] = dt   # w  += vw * dt
        F[3, 7] = dt   # h  += vh * dt
        self.register_buffer("F", F)

        # 观测矩阵 H: 只观测 bbox 4 维, 不直接观测速度
        H = torch.zeros(4, 8)
        H[:4, :4] = torch.eye(4)
        self.register_buffer("H", H)

        # 过程噪声 Q (恒速模型不准确所致). 速度维方差大于位置维.
        Q = torch.eye(8) * process_noise
        Q[4:, 4:] *= 4.0
        if learnable_noise:
            self.log_Q_diag = nn.Parameter(torch.log(Q.diag()))
        else:
            self.register_buffer("Q", Q)
        self.learnable_noise = learnable_noise

        # 观测噪声基准 R_base (4×4)
        R_base = torch.eye(4) * base_obs_noise
        if learnable_noise:
            self.log_R_base_diag = nn.Parameter(torch.log(R_base.diag()))
        else:
            self.register_buffer("R_base", R_base)

    @property
    def Q_matrix(self) -> torch.Tensor:
        if self.learnable_noise:
            return torch.diag(torch.exp(self.log_Q_diag))
        return self.Q

    @property
    def R_base_matrix(self) -> torch.Tensor:
        if self.learnable_noise:
            return torch.diag(torch.exp(self.log_R_base_diag))
        return self.R_base

    def predict(self, state: KalmanState) -> KalmanState:
        """预测步: 基于运动模型推进一帧."""
        F = self.F.to(state.x.device)
        Q = self.Q_matrix.to(state.x.device)
        new_x = F @ state.x
        new_P = F @ state.P @ F.T + Q
        return KalmanState(
            x=new_x, P=new_P,
            initialized=True,
            last_update_time=state.last_update_time,
        )

    def _obs_noise(
        self,
        confidence: float,
        view_angle_factor: float = 1.0,
    ) -> torch.Tensor:
        """
        计算单个观测的噪声协方差 R_k.

        Args:
            confidence:        [0, 1] 该增量的置信度
            view_angle_factor: [0.5, 2.0] 视角几何因子
                               1.0 = 正面观测; 2.0 = 极端斜视;
                               0.5 = 远比请求方更优视角.
        """
        R_base = self.R_base_matrix
        # 越自信 → R 越小; 视角越差 → R 越大
        scale = (1.0 / max(confidence, 0.05)) * view_angle_factor
        return R_base * scale

    def update_single(
        self,
        state: KalmanState,
        obs_bbox: torch.Tensor,
        confidence: float = 1.0,
        view_angle_factor: float = 1.0,
    ) -> KalmanState:
        """
        单个观测的卡尔曼更新 (增量融合接口).

        这是与 PKCI progressive_update() 对应的核心方法 ——
        一个新增量到达就调一次, 立即更新状态.
        """
        device = state.x.device
        H = self.H.to(device)
        R = self._obs_noise(confidence, view_angle_factor).to(device)

        # innovation
        z = obs_bbox.to(device)
        y = z - H @ state.x

        # innovation covariance
        S = H @ state.P @ H.T + R

        # Kalman gain
        K = state.P @ H.T @ torch.linalg.inv(S)

        # update state
        new_x = state.x + K @ y
        I = torch.eye(8, device=device)
        new_P = (I - K @ H) @ state.P

        return KalmanState(
            x=new_x, P=new_P,
            initialized=True,
            last_update_time=state.last_update_time,
        )

    def fuse_observations(
        self,
        local_bbox: torch.Tensor,
        local_confidence: float,
        prior_state: Optional[KalmanState],
        remote_bboxes: List[torch.Tensor],
        remote_confidences: List[float],
        view_angle_factors: Optional[List[float]] = None,
    ) -> Tuple[KalmanState, dict]:
        """
        一次性融合本地 + 多个远程观测 (训练时使用).

        步骤:
          1. predict (从 prior 推进一步)
          2. 用本地 bbox 做第一次 update
          3. 对每个 remote_bbox 做 update (顺序无关)
          4. 输出最终 state 的前 4 维作为 fused bbox

        Args:
            local_bbox:        [4] (cx, cy, w, h)
            local_confidence:  [0, 1]
            prior_state:       上一帧的卡尔曼状态 (None = 首帧, 用 local_bbox 初始化)
            remote_bboxes:     远程 bbox 列表, 每个 [4]
            remote_confidences:对应置信度列表
            view_angle_factors:对应视角因子列表 (默认全 1.0)

        Returns:
            new_state:  更新后的 KalmanState
            info:       dict with fused_bbox, num_observations, log
        """
        if view_angle_factors is None:
            view_angle_factors = [1.0] * len(remote_bboxes)

        # Step 1: predict
        if prior_state is None or not prior_state.initialized:
            state = KalmanState.from_bbox(local_bbox)
        else:
            state = self.predict(prior_state)

        # Step 2: local update
        state = self.update_single(state, local_bbox, local_confidence, 1.0)

        # Step 3: 顺序无关地更新所有远程观测
        for bbox, conf, va in zip(remote_bboxes, remote_confidences, view_angle_factors):
            state = self.update_single(state, bbox, conf, va)

        fused_bbox = state.x[:4].clone()
        # bbox 平滑约束: 如果融合后变化过大, 限幅 (与 local 偏差 < 50%)
        diff = (fused_bbox - local_bbox).abs()
        max_change = local_bbox[2:4].repeat(2) * 0.5  # 50% of w/h
        clipped_diff = torch.clamp(diff, max=max_change)
        sign = torch.sign(fused_bbox - local_bbox)
        fused_bbox = local_bbox + sign * clipped_diff

        info = {
            "fused_bbox": fused_bbox,
            "num_observations": 1 + len(remote_bboxes),
            "innovation_norm": float(torch.norm(state.x[:4] - local_bbox).item()),
        }
        return state, info

    @torch.no_grad()
    def progressive_update(
        self,
        current_state: KalmanState,
        new_bbox: torch.Tensor,
        new_confidence: float,
        view_angle_factor: float = 1.0,
    ) -> KalmanState:
        """
        增量融合: 在现有状态上吸收一个新到达的远程 bbox 观测.
        与 PKCI progressive_update() 对应 — 运行时最常用的接口.

        与 PKCI 不同: 此处 "权重" 通过 R_k 隐式编码 (越自信 R 越小),
        而不是显式的加权平均权重.
        """
        return self.update_single(
            current_state, new_bbox, new_confidence, view_angle_factor,
        )
