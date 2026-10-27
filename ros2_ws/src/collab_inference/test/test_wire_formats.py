import numpy as np

from collab_infer.pkci import LocalResult, decode_increment, encode_increment, fuse
from collab_infer.pkct import decode_identity, encode_identity


def test_pkci_increment_round_trip_and_fusion():
    logits = np.linspace(-2.0, 2.0, 44, dtype=np.float32)
    result = LocalResult(logits, logits, 43, 0.1, 0.8, 1.0)
    payload = encode_increment(result, drone_id=2, frame_id=17, level="low")
    decoded = decode_increment(payload)
    assert decoded.drone_id == 2
    assert decoded.frame_id == 17
    assert decoded.log_probs.shape == (44,)
    assert fuse([decoded.log_probs]).views == 1


def test_pkct_identity_round_trip():
    embedding = np.arange(1, 129, dtype=np.float32)
    embedding /= np.linalg.norm(embedding)
    payload = encode_identity(embedding, 0.75, drone_id=3, frame_id=99)
    decoded = decode_identity(payload)
    assert decoded.drone_id == 3
    assert decoded.frame_id == 99
    assert decoded.embedding.shape == (128,)
    assert np.isclose(np.linalg.norm(decoded.embedding), 1.0, atol=1e-5)
