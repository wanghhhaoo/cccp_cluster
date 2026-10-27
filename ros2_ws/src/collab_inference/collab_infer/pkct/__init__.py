"""PKCT 多机协同单目标跟踪：单机跟踪、身份增量编解码、主跟选举。

每架无人机用自己的相机独立跟踪同一个目标（"本地优先"：协同结果不回灌本地
跟踪）。每帧把目标的身份特征（128 维 ReID embedding 的前 k 维，int8 量化）
连同置信度广播出去，12 + k 字节。每架无人机用收到的身份增量维护一份共享
Gallery，并在其上做去中心化主跟选举：锁定质量 = 置信度 × 与 Gallery 的匹配
分，取最大者并加迟滞。所有无人机看到同样的增量就会选出同一个主跟，整个机群
本帧的答案就是主跟自己的跟踪框。

    from collab_infer.pkct import PkctTracker, CollabNode, encode_identity, decode_identity

    tracker = PkctTracker(device="cuda:0")
    tracker.init(first_frame, box_xywh)          # 起始帧 + 目标框
    node = CollabNode()
    for t, frame in frames:
        r = tracker.track(frame)
        payload = encode_identity(r.embedding, r.confidence, drone_id, t)
        broadcast(payload)
        heard = [decode_identity(payload)] + [decode_identity(p) for p in received(t)]
        primary = node.step(heard)               # 本帧主跟的 drone_id
"""

import json
import pathlib
import struct
import time
from collections import namedtuple

import numpy as np

from .. import check_finite

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_WEIGHTS = PACKAGE_ROOT / "weights" / "pkct_b0bgt.pt"
DEFAULT_CALIBRATION = PACKAGE_ROOT / "weights" / "pkct_b0bgt.calib.json"
TEMPLATE_SIZE = 128
SEARCH_SIZE = 256
TEMPLATE_FACTOR = 2.0   # 模板裁剪边长 = 目标长边 × 2
SEARCH_FACTOR = 4.0     # 搜索区边长 = 上一帧目标长边 × 4
EMBED_DIM = 128

# 带宽等级 -> 身份前缀长度 k（Matryoshka 训练，前 k 维本身就是有效特征）。
LEVEL_TO_K = {"full": 128, "high": 64, "medium": 32, "low": 16, "minimal": 8}
LEVELS = tuple(LEVEL_TO_K)

# 头部：drone_id u8, frame_id u32, confidence f16, k u8, scale f32 = 12 字节，小端；
# 之后是 k 个 int8（embedding 前 k 维 / scale × 127 取整）。
HEADER = struct.Struct("<BIeBf")

TrackResult = namedtuple("TrackResult", [
    "box_xywh",     # (x, y, w, h) 浮点像素，左上角 + 宽高，已裁到图像范围内
    "confidence",   # float  跟踪置信度
    "embedding",    # np.float32 [128]  目标身份特征，L2 归一化
    "latency_ms",   # float  本次前向耗时（不含读图、裁剪）
])

Identity = namedtuple("Identity", [
    "drone_id", "frame_id",
    "confidence",   # float  发送方跟踪置信度
    "k",            # int    携带的特征维数
    "embedding",    # np.float32 [k]  反量化后重新 L2 归一化
])


def _to_chw_uint8(frame):
    """HxWx3 uint8 RGB numpy / PIL.Image / 文件路径 -> [3, H, W] uint8 张量。"""
    import torch
    from PIL import Image
    if not isinstance(frame, np.ndarray):
        if not isinstance(frame, Image.Image):
            frame = Image.open(frame)
        frame = np.asarray(frame.convert("RGB"))
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError("frame must be HxWx3 uint8 RGB")
    if not frame.flags.writeable or not frame.flags.c_contiguous:
        frame = np.array(frame)
    return torch.from_numpy(frame).permute(2, 0, 1)


def _crop(image, center, size, out_size):
    """以 center 为中心裁 size×size 的正方形（越界部分复制边缘），缩放到
    out_size，像素值缩放到 [0, 1]。与 pkct.data.datasets._crop 相同。"""
    import torch.nn.functional as F
    _, height, width = image.shape
    half = size / 2
    x1 = int(round(center[0] - half))
    y1 = int(round(center[1] - half))
    x2 = int(round(center[0] + half))
    y2 = int(round(center[1] + half))
    pad_l = max(0, -x1); pad_t = max(0, -y1)
    pad_r = max(0, x2 - width); pad_b = max(0, y2 - height)
    if pad_l or pad_t or pad_r or pad_b:
        image = F.pad(image.unsqueeze(0).float(),
                      (pad_l, pad_r, pad_t, pad_b), mode="replicate").squeeze(0)
        x1 += pad_l; x2 += pad_l
        y1 += pad_t; y2 += pad_t
    cropped = image[:, y1:y2, x1:x2].float() / 255.0
    return F.interpolate(cropped.unsqueeze(0), size=(out_size, out_size),
                         mode="bilinear", align_corners=False).squeeze(0)


class PkctTracker(object):
    """单机单目标跟踪（MixFormer-Lite 主干 + 专家修正 + 时序记忆 + 卡尔曼平滑）。

    一个实例跟一个目标；有状态，必须先 init() 再逐帧 track()。
    """

    def __init__(self, weights=None, device="cpu"):
        import torch
        from ._vendor.models.pkct_system import PKCTSystem
        self.torch = torch
        self.device = torch.device(device)
        self.model = PKCTSystem(embed_dim=192, feature_dim=256, reid_dim=128,
                                num_experts=3)
        state = torch.load(str(weights or DEFAULT_WEIGHTS), map_location="cpu",
                           weights_only=True)
        self.model.load_state_dict(state.get("drone_0", state))
        self.model.to(self.device).eval()
        self.template = None
        self.previous = None    # 上一帧目标 (cx, cy, w, h)

    def init(self, frame, box_xywh):
        """用起始帧和目标框 (x, y, w, h)（左上角 + 宽高，像素）开始一段新跟踪。"""
        torch = self.torch
        x, y, w, h = [float(v) for v in box_xywh]
        if w <= 0 or h <= 0:
            raise ValueError("box width and height must be positive")
        self.model.reset()
        image = _to_chw_uint8(frame)
        self.previous = torch.tensor([x + w / 2, y + h / 2, w, h])
        self.template = _crop(image, (x + w / 2, y + h / 2),
                              max(w, h) * TEMPLATE_FACTOR,
                              TEMPLATE_SIZE).unsqueeze(0).to(self.device)

    def track(self, frame):
        """下一帧 -> TrackResult。帧要按时间顺序送入，不能跳回。"""
        torch = self.torch
        if self.template is None:
            raise RuntimeError("call init() before track()")
        image = _to_chw_uint8(frame)
        _, height, width = image.shape
        previous = self.previous
        centre_x = min(max(previous[0].item(), 0.0), float(width))
        centre_y = min(max(previous[1].item(), 0.0), float(height))
        size = min(max(max(previous[2].item(), previous[3].item()) *
                       SEARCH_FACTOR, 16.0), 2.0 * max(height, width))
        search = _crop(image, (centre_x, centre_y), size,
                       SEARCH_SIZE).unsqueeze(0).to(self.device)
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        began = time.perf_counter()
        with torch.no_grad():
            output = self.model.forward_local_only(
                self.template, search, use_experts=True, use_temporal=True)
        box = output["bbox"].detach().float().cpu()
        check_finite(torch, box, self.device)
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        elapsed = (time.perf_counter() - began) * 1000
        # 搜索区坐标 -> 原图坐标
        scale = SEARCH_SIZE / size
        cx = min(max((box[0].item() - SEARCH_SIZE / 2) / scale + centre_x, 0.0),
                 float(width))
        cy = min(max((box[1].item() - SEARCH_SIZE / 2) / scale + centre_y, 0.0),
                 float(height))
        w = min(max(box[2].item() / scale, 2.0), float(width))
        h = min(max(box[3].item() / scale, 2.0), float(height))
        self.previous = torch.tensor([cx, cy, w, h])
        embedding = output["reid_embed"].detach().float().cpu().reshape(-1)
        return TrackResult((cx - w / 2, cy - h / 2, w, h),
                           float(output["confidence"]),
                           embedding.numpy().astype(np.float32), elapsed)


def encode_identity(embedding, confidence, drone_id, frame_id, level="full"):
    """身份增量 bytes（12 + k 字节：full 140 B，high 76，medium 44，low 28，minimal 20）。"""
    embedding = np.asarray(embedding, dtype=np.float32).reshape(-1)
    k = min(LEVEL_TO_K[level], embedding.shape[0])
    prefix = embedding[:k]
    scale = max(float(np.abs(prefix).max()), 1e-6)
    code = np.clip(np.round(prefix / scale * 127.0), -127, 127).astype(np.int8)
    return HEADER.pack(drone_id, frame_id, confidence, k, scale) + code.tobytes()


def decode_identity(payload):
    """身份增量 bytes -> Identity。格式不对时抛 ValueError。"""
    if len(payload) < HEADER.size:
        raise ValueError("identity increment shorter than its header")
    drone_id, frame_id, confidence, k, scale = HEADER.unpack_from(payload)
    if k < 1 or len(payload) != HEADER.size + k:
        raise ValueError("malformed identity increment")
    code = np.frombuffer(payload, np.int8, k, HEADER.size).astype(np.float32)
    prefix = code / 127.0 * scale
    prefix = prefix / max(float(np.linalg.norm(prefix)), 1e-12)
    return Identity(drone_id, frame_id, float(confidence), k,
                    prefix.astype(np.float32))


def load_calibration(path=None):
    """Gallery 阈值标定（随权重一起给出；换权重要重新标定）。"""
    return json.loads(pathlib.Path(path or DEFAULT_CALIBRATION).read_text())


class CollabNode(object):
    """每架无人机一个：共享 Gallery 副本 + 去中心化主跟选举。

    每帧调用一次 step()，传入本帧听到的全部身份增量（包括自己的——把自己发出的
    bytes 解码后放进去，保证各机看到的输入一致）。第一次调用用这些增量初始化
    Gallery 锚点（各视角首帧都视为可信），之后门控写入：置信度足够、与锚点一致、
    且不与已有条目重复才写入，防止干扰物污染。
    """

    def __init__(self, calibration=None, margin=0.1, hysteresis=3):
        from ._vendor.collab_system import Gallery, PrimaryElection
        calibration = calibration or load_calibration()
        self.gallery = Gallery(
            anchor_consistency_thresh=calibration["diff_p75"],
            dup_thresh=calibration["same_p90"])
        self.election = PrimaryElection(margin=margin, hysteresis=hysteresis,
                                        margin_mode="rel")
        self.seeded = False

    def step(self, identities):
        """本帧身份增量列表 -> 主跟 drone_id（列表为空且尚无主跟时为 None）。"""
        import torch
        import torch.nn.functional as F
        observations = [(i.drone_id, torch.from_numpy(i.embedding.copy()),
                         i.confidence) for i in identities]
        if not self.seeded and observations:
            width = max(e.shape[-1] for _, e, _ in observations)
            full = [e for _, e, _ in observations if e.shape[-1] == width]
            self.gallery.set_anchor(F.normalize(torch.stack(full).mean(0), dim=-1))
            for _, embedding, _ in observations:
                self.gallery.entries.append(embedding.clone())
            self.seeded = True
        lock = {drone: confidence * max(0.0, self.gallery.match(embedding))
                for drone, embedding, confidence in observations}
        primary = self.election.update(lock)
        for _, embedding, confidence in observations:
            self.gallery.write(embedding, confidence)
        return primary

    @property
    def primary(self):
        return self.election.primary
