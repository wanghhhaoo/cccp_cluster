"""PKCI 多视角协同分类：单机推理、知识增量编解码、多视角融合。

每架无人机对同一目标的自己那一个视角做本地推理，得到 44 类的对数后验；
把它压缩成"知识增量"（Top-K 对数后验，13 + 4K 字节）广播给其它无人机；
收到的增量与自己的结果一起融合，得到协同分类结果。

融合与顺序无关、对任意子集有效：少收到几个视角只是融合的视角少，不会出错。

    from collab_infer.pkci import PkciClassifier, encode_increment, decode_increment, fuse

    model = PkciClassifier(device="cuda:0")
    own = model.infer(image)                                    # 本机视角
    payload = encode_increment(own, drone_id=0, frame_id=t)     # 发出去的 bytes
    peers = [decode_increment(p) for p in received_payloads]    # 收到的 bytes
    result = fuse([own.log_probs] + [p.log_probs for p in peers])
"""

import math
import os
import pathlib
import struct
import time
from collections import namedtuple

import numpy as np

from .. import check_finite

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_WEIGHTS = PACKAGE_ROOT / "weights" / "pkci_mvpn_plong_s2.pt"
NUM_CLASSES = 44
INPUT_SIZE = 224
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)

# 带宽等级 -> 增量里保留的类别数 K（> 44 时取 44）。增量大小 = 13 + 4K 字节。
LEVEL_TO_K = {"full": 100, "high": 20, "medium": 10, "low": 5,
              "minimal": 3, "crisis": 1}
LEVELS = tuple(LEVEL_TO_K)

# 头部：drone_id u8, expert u8 (0 = general), frame_id u32, confidence f16,
# top_k u8, total_classes u16, reliability f16 = 13 字节，小端。
# 与 pkci 原 FragmentEncoder.serialize 布局相同（原实现第 2 字节是专家类型
# 名的进程内 hash、第 3 字段是毫秒时间戳；这里固定为 0 与帧号）。
HEADER = struct.Struct("<BBIeBHe")

LocalResult = namedtuple("LocalResult", [
    "logits",       # np.float32 [44]  温度校准后的 logits
    "log_probs",    # np.float32 [44]  log_softmax(logits)，融合用这个
    "label",        # int              本机预测类别 0..43
    "score",        # float            本机预测类别的 softmax 概率
    "confidence",   # float            模型不确定性头输出的自信度 ∈ [0, 1]
    "latency_ms",   # float            本次前向耗时（不含预处理）
])

Increment = namedtuple("Increment", [
    "drone_id", "frame_id",
    "confidence",   # float  发送方自信度
    "reliability",  # float  发送方自估可靠性 = 1 - 归一化熵
    "top_k",        # int    实际携带的类别数
    "log_probs",    # np.float32 [44]  Top-K 之外的类别用最大熵补全，是合法分布
])

FusedResult = namedtuple("FusedResult", [
    "label",        # int    融合后的类别
    "score",        # float  该类别的融合概率（归一化后）
    "log_probs",    # np.float32 [44]  融合后的对数分数（未归一化）
    "views",        # int    参与融合的视角数（含自己）
])


def class_names():
    """MVP-N 44 个类别的目录名，下标即类别号。"""
    import json
    return json.loads((pathlib.Path(__file__).with_name("classes.json")).read_text())


class PkciClassifier(object):
    """单机推理：EfficientNet-B0 主干 + 通用专家头（44 类，MVP-N 训练）。"""

    def __init__(self, weights=None, device="cpu"):
        import torch
        from ._vendor.backbone import Backbone
        from ._vendor.expert import ExpertHead, ExpertType
        self.torch = torch
        self.device = torch.device(device)
        checkpoint = torch.load(str(weights or DEFAULT_WEIGHTS),
                                map_location="cpu", weights_only=True)
        self.backbone = Backbone(num_classes=NUM_CLASSES)
        self.backbone.load_state_dict(checkpoint["backbone"])
        self.head = ExpertHead(512, NUM_CLASSES, ExpertType.GENERAL)
        self.head.load_state_dict(checkpoint["head"])
        self.backbone.to(self.device).eval()
        self.head.to(self.device).eval()

    def preprocess(self, image):
        """RGB 图像 -> [3, 224, 224] 张量：短边缩放到 224、中心裁剪、ImageNet 归一化。

        image：PIL.Image，或 HxWx3 uint8 RGB 的 numpy 数组，或图像文件路径。
        """
        import torchvision.transforms as T
        from PIL import Image
        if isinstance(image, (str, os.PathLike)):
            image = Image.open(image)
        elif isinstance(image, np.ndarray):
            image = Image.fromarray(image)
        transform = T.Compose([T.Resize(INPUT_SIZE), T.CenterCrop(INPUT_SIZE),
                               T.ToTensor(), T.Normalize(MEAN, STD)])
        return transform(image.convert("RGB"))

    def infer(self, image):
        """一张图 -> LocalResult。"""
        return self.infer_batch([image])[0]

    def infer_batch(self, images):
        """多张图一次前向 -> [LocalResult]（每张图的 latency_ms 为整批耗时）。"""
        torch = self.torch
        batch = torch.stack([self.preprocess(i) for i in images]).to(self.device)
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        began = time.perf_counter()
        with torch.no_grad():
            output = self.head(self.backbone(batch)["feature"])
            logits = output["logits"].float()
            confidence = output["confidence"].float().reshape(-1)
        check_finite(torch, logits, self.device)
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        elapsed = (time.perf_counter() - began) * 1000
        log_probs = torch.log_softmax(logits, -1).cpu().numpy()
        logits = logits.cpu().numpy()
        results = []
        for i in range(len(images)):
            label = int(log_probs[i].argmax())
            results.append(LocalResult(
                logits=logits[i], log_probs=log_probs[i], label=label,
                score=float(np.exp(log_probs[i][label])),
                confidence=float(confidence[i]), latency_ms=elapsed))
        return results


def _log_softmax(x):
    x = np.asarray(x, dtype=np.float32)
    shifted = x - x.max()
    return shifted - np.log(np.exp(shifted).sum())


def encode_increment(result, drone_id, frame_id, level="full", confidence=None):
    """本机结果 -> 知识增量 bytes（13 + 4K 字节）。

    result：LocalResult，或 [44] 的 logits / log_probs 数组（此时需给 confidence）。
    drone_id：0..255；frame_id：0..2^32-1，接收方据此把增量对到同一帧。
    level：LEVELS 之一，决定 K（full 44 类 189 B，high 93 B，medium 53 B，
           low 33 B，minimal 25 B，crisis 17 B）。
    """
    if isinstance(result, LocalResult):
        logits = result.logits
        confidence = result.confidence if confidence is None else confidence
    else:
        logits = result
        if confidence is None:
            raise ValueError("confidence is required when passing an array")
    log_probs = _log_softmax(logits)
    classes = log_probs.shape[-1]
    k = min(LEVEL_TO_K[level], classes)
    order = np.argsort(-log_probs, kind="stable")[:k]
    probs = np.exp(log_probs)
    entropy = float(-(probs * log_probs).sum())
    reliability = (min(max(1.0 - entropy / math.log(classes), 0.0), 1.0)
                   if classes > 1 else 1.0)
    header = HEADER.pack(drone_id, 0, frame_id, confidence, k, classes,
                         reliability)
    return (header + order.astype("<i2").tobytes() +
            log_probs[order].astype("<f2").tobytes())


def decode_increment(payload, num_classes=NUM_CLASSES):
    """知识增量 bytes -> Increment。格式不对时抛 ValueError。"""
    if len(payload) < HEADER.size:
        raise ValueError("knowledge increment shorter than its header")
    drone_id, _, frame_id, confidence, k, total, reliability = \
        HEADER.unpack_from(payload)
    if total != num_classes or k < 1 or k > total or \
            len(payload) != HEADER.size + 4 * k:
        raise ValueError("malformed knowledge increment")
    indices = np.frombuffer(payload, "<i2", k, HEADER.size).astype(np.int64)
    values = np.frombuffer(payload, "<f2", k, HEADER.size + 2 * k).astype(np.float32)
    full = np.empty(total, dtype=np.float32)
    if total > k:
        # 最大熵补全：Top-K 之外的剩余概率质量均摊到其余类别。
        mass = min(float(np.exp(values).sum()), 1.0 - 1e-6)
        full.fill(math.log(max(1.0 - mass, 1e-9) / (total - k)))
    else:
        full.fill(0.0)
    full[indices] = values
    return Increment(drone_id, frame_id, float(confidence), float(reliability),
                     k, full)


def fuse(log_probs_list):
    """多视角融合（pkci "trim" 规则，即协同实验所用规则）。

    >= 4 个视角：逐类去掉最大值和最小值后取平均（截尾均值，抗个别视角出错）；
    1-3 个视角：按熵加权的几何平均（越确定的视角权重越大）。
    与输入顺序无关；只有自己一个视角时结果等于本机结果。
    """
    stack = np.stack([np.asarray(x, dtype=np.float32) for x in log_probs_list])
    views, classes = stack.shape
    if views >= 4:
        fused = np.sort(stack, axis=0)[1:-1].mean(0)
    else:
        entropy = -(np.exp(stack) * stack).sum(-1)
        weight = np.maximum(1.0 - entropy / math.log(classes), 1e-6)
        fused = (stack * weight[:, None]).sum(0) / weight.sum()
    label = int(fused.argmax())
    normalized = fused - fused.max()
    score = float(np.exp(normalized[label]) / np.exp(normalized).sum())
    return FusedResult(label, score, fused.astype(np.float32), views)
