"""PKCI 协同分类与 PKCT 协同跟踪的推理包，见 README.md。"""


def check_finite(torch, tensor, device):
    """模型输出含 NaN/Inf 时报错，而不是把错误结果交给上层。

    已知情况：AGX Orin 上 NVIDIA 的 torch 2.1（nv23.06）在 CPU 上跑 EfficientNet 会输出 NaN，
    随机初始化的 torchvision efficientnet_b0 即可复现，与本包无关。Jetson 上请用 GPU（cuda:0）。
    """
    if not bool(torch.isfinite(tensor).all()):
        raise RuntimeError(
            "model produced NaN/Inf on {}; on Jetson use device='cuda:0' "
            "(NVIDIA torch 2.1 CPU kernels are known to produce NaN here)".format(device))
