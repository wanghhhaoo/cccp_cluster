"""One CCCP role per process. CCCP_JOB_STORE is mounted on all six nodes."""
import os
from pathlib import Path
import sys
import time

import numpy as np
import torch

root = Path(os.environ["HETEROMAP_ROOT"]).resolve()
sys.path.insert(0, str(root))
from fusion_node import FusionNode
from run_20frame_demo import read_sequence
from runtime import UAVS
from uav_node import UavNode


def wait(path):
    deadline = time.monotonic() + float(os.getenv("HETEROMAP_TIMEOUT", "180"))
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(str(path))
        time.sleep(0.1)
    return path


def send(obj, path):
    tmp = path.with_suffix(".part")
    torch.save(obj, tmp)
    tmp.replace(path)


def receive(path):
    obj = torch.load(wait(path), map_location="cpu", weights_only=True)
    path.unlink()
    return obj


def main(run, role):
    rows = read_sequence(root)
    device = os.getenv("COLLAB_DEVICE", "cuda:0")
    if role == "primary":
        node = FusionNode(root, root, device)
    else:
        node = UavNode(role, root, root, device)
        datasets, _ = node.training.build_datasets(node.args)
        dataset = datasets["val"]
        index = {token: i for i, token in enumerate(dataset.keep_tokens)}
    for n, row in enumerate(rows):
        token = row["sample_token"]
        folder = run / f"frame_{n:02d}"
        folder.mkdir(exist_ok=True)
        if role == "primary":
            packets = {u: receive(folder / f"{u}.pt") for u in UAVS}
            if any(p["frame_id"] != token or p["uav"] != u for u, p in packets.items()):
                raise ValueError("packet identity mismatch")
            result = node.fuse(packets)
            for u, context in result.pop("contexts").items():
                send(context, folder / f"context_{u}.pt")
            np.savez_compressed(run / role / f"{n:02d}.npz", **result)
            for u in UAVS:
                wait(folder / f"{u}.done")
        else:
            item = dataset[index[token]][node.training._bev_key(role)]
            frame = {key: item[key] for key in ("images", "intrinsics", "extrinsics")}
            frame.update(frame_id=token, pose=item["meta"]["pose"], valid_bev_mask=~item["ignore"])
            packet, state = node.encode(frame)
            send(packet, folder / f"{role}.pt")
            result = node.decode(state, receive(folder / f"context_{role}.pt"))
            np.savez_compressed(run / role / f"{n:02d}.npz", **result)
            (folder / f"{role}.done").touch()


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
