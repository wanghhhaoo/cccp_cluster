# Collaborative inference ROS 2 services

This package exposes the supplied PKCI collaborative classifier and PKCT
collaborative tracker as ROS 2 services. Image bytes stay in `sensor_msgs/Image`;
the compact cross-drone increments are returned as `uint8[]` so the edge agent
can send them through LCRPC or another transport without changing their wire
format.

## Services

| Service | Type | Purpose |
|---|---|---|
| `/algorithms/pkci/classify` | `Classify` | Local classification and PKCI increment generation |
| `/algorithms/pkci/fuse` | `FuseClassification` | Fuse local posterior with peer increments |
| `/algorithms/pkct/init` | `InitTracker` | Create/reset a stateful tracking session |
| `/algorithms/pkct/track` | `Track` | Track one frame and generate identity increment |
| `/algorithms/pkct/elect` | `ElectPrimary` | Update collaborative primary-drone election |
| `/algorithms/pkct/reset` | `ResetTracker` | Release tracking/election state |

Set `device:=cuda:0` on Jetson. The Jetson runtime must use NVIDIA's matching
PyTorch and torchvision wheels; do not install generic ARM wheels from PyPI.

PKCT sessions are keyed by `tracker_id`. Calls for a session must be ordered by
frame. PKCI fusion and PKCT election reject peer payloads whose embedded
`frame_id` or `drone_id` disagrees with the ROS message metadata.
