"""
CollabSystem: v2 协同的"算法半"——共享 Gallery + 去中心化主跟选举

设计文档 §5。这一层不含可训练参数,跑在身份 embedding + 置信度之上。

- Gallery:共享多视角身份模型,每架一份副本(从广播流构建)。写入由"质量 +
  锚一致性"双闸门控(抗投毒),按多样性保留(非 FIFO),永久保留初始模板锚。
- PrimaryElection:去中心化主跟选举。锁定质量 = 置信度 × Gallery 匹配分,
  argmax + 迟滞。统一交接(锁定转移)与重捕(无候选→搜索填回)。
- CollabNode:每架无人机一个,持有 Gallery 副本 + 选举;每帧从广播流摄入观测。

可用 data/collab_sim.py 的场景在 embedding 级验证(真多视角数据到位前)。
"""

from typing import Dict, List, Optional

import torch
import torch.nn.functional as F


def _cos(a: torch.Tensor, b: torch.Tensor) -> float:
    """余弦相似度。维度不等时取共同前缀 + 重新归一化(支持压缩的 Matryoshka 前缀
    查询匹配全维库条目)。"""
    if a.shape[-1] == b.shape[-1]:
        return float(torch.dot(a, b))   # 同维且已 L2-norm
    d = min(a.shape[-1], b.shape[-1])
    return float(torch.dot(F.normalize(a[:d], dim=-1), F.normalize(b[:d], dim=-1)))


class Gallery:
    """共享多视角身份模型(每架一份副本)。"""

    # 默认阈值按 pkct_final 尺度定; 换权重后 embedding 相似度尺度会整体平移
    # (实测: pkct_b0m 的"不同目标"均值 0.27→0.55), 硬编码阈值会失准 ——
    # 用 from_calibration() 按该权重实测分布标定, 见 utils/collab_eval --dump-calibration。
    def __init__(
        self,
        max_size: int = 16,
        write_quality_thresh: float = 0.5,
        anchor_consistency_thresh: float = 0.2,
        dup_thresh: float = 0.9,
    ):
        self.max_size = max_size
        self.q_thresh = write_quality_thresh
        self.anchor_thresh = anchor_consistency_thresh
        self.dup_thresh = dup_thresh
        self.entries: List[torch.Tensor] = []   # entries[0] = 锚(永久保留)
        self.anchor: Optional[torch.Tensor] = None

    @classmethod
    def from_calibration(cls, calib: dict, **kw):
        """按某权重实测的相似度分布标定阈值(尺度自适应)。

        calib 来自 `collab_eval --dump-calibration`:
          anchor 一致性门 = 不同目标 p75(挡住 3/4 干扰物, 又不误杀多数真观测);
          近重复门 = 同目标 p90(该权重下"几乎同一视角"的水平)。
        """
        kw.setdefault("anchor_consistency_thresh", calib["diff_p75"])
        kw.setdefault("dup_thresh", calib["same_p90"])
        return cls(**kw)

    def set_anchor(self, emb: torch.Tensor):
        """首帧注册初始模板锚(可信)。"""
        self.anchor = emb.clone()
        self.entries = [emb.clone()]

    def match(self, emb: torch.Tensor) -> float:
        """身份匹配分:与库内最大余弦(∈[-1,1])。空库返回 0。"""
        if not self.entries:
            return 0.0
        return max(_cos(emb, e) for e in self.entries)

    def write(self, emb: torch.Tensor, confidence: float) -> bool:
        """
        门控写入(富化)。返回是否写入。
        双闸抗投毒:质量门控 + 锚一致性(与可信锚差异过大则拒,挡住干扰物)。
        多样性:近重复(已被覆盖)不写;满则淘汰最冗余的非锚条目。
        """
        if confidence < self.q_thresh:
            return False
        if self.anchor is not None and _cos(emb, self.anchor) < self.anchor_thresh:
            return False                      # 抗投毒:偏离锚太远 → 疑似干扰物
        if self.match(emb) > self.dup_thresh:
            return False                      # 近重复,避免冗余写入(抗近因坍缩)
        if len(self.entries) >= self.max_size:
            self._evict_most_redundant()
        self.entries.append(emb.clone())
        return True

    def _evict_most_redundant(self):
        """淘汰与其它条目最相似的非锚条目(保多样性,锚 index 0 永久保留)。"""
        if len(self.entries) <= 1:
            return
        best_i, best_red = None, -2.0
        for i in range(1, len(self.entries)):
            red = max(_cos(self.entries[i], self.entries[j])
                      for j in range(len(self.entries)) if j != i)
            if red > best_red:
                best_red, best_i = red, i
        if best_i is not None:
            self.entries.pop(best_i)


class PrimaryElection:
    """去中心化主跟选举:锁定质量 argmax + 迟滞。"""

    # ⚠️ margin 是**绝对**阈值, 而锁定质量 = 置信 × Gallery 匹配分, 其尺度随 ReID
    # 权重漂移(与 Gallery 写入门/锚一致门同一类问题)。margin=0.1 在新尺度下约占
    # 典型锁定质量的 15-30%, 形成很强的**在位优势**: 先当上主跟的那架会一直连任,
    # 即使它平均跟得最差(实测 drone0 连续率 0.426 最低, 却被过度选中)。
    # margin_mode="rel" 改为相对比较(尺度无关), 推荐用它。
    def __init__(self, margin: float = 0.1, hysteresis: int = 3,
                 margin_mode: str = "abs"):
        self.margin = margin
        self.hysteresis = hysteresis
        self.margin_mode = margin_mode
        self.primary: Optional[int] = None
        self._count = 0   # 主跟"连续被击败"的帧数

    def update(self, lock_qualities: Dict[int, float]) -> Optional[int]:
        """
        lock_qualities: 在线无人机 → 锁定质量(置信 × Gallery 匹配分)。
        返回当前主跟 id。
        """
        if not lock_qualities:
            return self.primary                       # 全丢:保持(上层可触发重捕)
        best = max(lock_qualities, key=lock_qualities.get)
        # 在位主跟掉线/不在候选 → 立即重选(重捕/掉线重选)
        if self.primary is None or self.primary not in lock_qualities:
            self.primary, self._count = best, 0
            return self.primary
        # 迟滞计"主跟连续被击败"的帧数(不绑定挑战者身份, 避免两个等价候选互相
        # 抖动导致计数清零); 到点切给当前最优者.
        inc = lock_qualities[self.primary]
        need = (inc * (1.0 + self.margin) if self.margin_mode == "rel"
                else inc + self.margin)
        if best != self.primary and lock_qualities[best] > need:
            self._count += 1
            if self._count >= self.hysteresis:
                self.primary, self._count = best, 0
        else:
            self._count = 0
        return self.primary


class CollabNode:
    """每架无人机的协同节点:Gallery 副本 + 选举,从广播流逐帧摄入。"""

    def __init__(self, drone_id: int, election_kw: Optional[dict] = None,
                 lock_mode: str = "product", **gallery_kw):
        self.drone_id = drone_id
        self.gallery = Gallery(**gallery_kw)
        self.election = PrimaryElection(**(election_kw or {}))
        # lock_mode="conf" 是消融对照(只用置信度), 用于检验设计 §5.4
        # "锁定质量须乘身份匹配分"的核心论证到底值多少。
        self.lock_mode = lock_mode

    def step(self, observations: list) -> Optional[int]:
        """
        observations: 本帧从广播听到的所有观测(含自己),来自 collab_sim.Observation。
        流程:算锁定质量(用当前 Gallery 作裁判)→ 选举 → 富化写入(副作用)。
        """
        # 1. 锁定质量 = 置信 × Gallery 匹配分(干扰物匹配低 → 自然落选)
        lock_q = {
            o.drone_id: (o.confidence if self.lock_mode == "conf" else
                         o.confidence * max(0.0, self.gallery.match(o.embedding)))
            for o in observations
        }
        # 2. 去中心化选举
        primary = self.election.update(lock_q)
        # 3. 富化:门控写入(抗投毒在 Gallery.write 内)
        for o in observations:
            self.gallery.write(o.embedding, o.confidence)
        return primary
