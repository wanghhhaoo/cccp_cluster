#!/bin/bash
set -eo pipefail
if [[ ${1:-} != --inside ]]; then
    output=$1
    # batch.py names the container so that it can remove it if this script is killed.
    name="${CCCP_FASTLIVO_CONTAINER:-cccp-fastlivo-$(cat /proc/sys/kernel/random/uuid)}"
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
    trap 'exit 143' TERM INT
    calibration=()
    if [[ -n ${FASTLIVO_CONFIG_DIR:-} ]]; then
        calibration=(--mount "type=bind,src=$FASTLIVO_CONFIG_DIR,dst=/calibration,readonly")
    fi
    docker run --rm --name "$name" --network none -e ROS_IP=127.0.0.1 -e ROS_MASTER_URI=http://127.0.0.1:11311 \
        --mount "type=bind,src=$FASTLIVO_BAG,dst=/input.bag,readonly" \
        --mount "type=bind,src=$output,dst=/root/lamm_ws/src/FAST-LIVO2/Log/PCD" \
        --mount "type=bind,src=$0,dst=/job.sh,readonly" \
        "${calibration[@]}" --entrypoint bash lamm_full:v1 /job.sh --inside &
    wait $!
    exit
fi
source /root/lamm_ws/devel/setup.bash
cfg=/root/lamm_ws/src/FAST-LIVO2/config
if [[ -d /calibration ]]; then
    cp /calibration/{avia.yaml,camera_pinhole.yaml} "$cfg/"
    cloud=all_downsampled_points.pcd
else
    sed -i 's/^  img_en: 1/  img_en: 0/' "$cfg/avia.yaml"
    cloud=all_raw_points.pcd
fi
sed -i -E 's/^  seq_name:.*/  seq_name: cccp_job/;s/^  pose_output_en:.*/  pose_output_en: true/' "$cfg/avia.yaml"
roslaunch fast_livo mapping_avia.launch rviz:=false > /tmp/mapping.log 2>&1 &
launcher=$!
trap 'tail -c 4096 /tmp/mapping.log' EXIT
for i in {1..90}; do
    rosnode info /laserMapping 2>/dev/null | grep -q /livox/lidar && break
    kill -0 "$launcher"
    sleep 1
done
rosnode ping -c 1 /laserMapping
rosbag play --quiet /input.bag
rosnode ping -c 1 /laserMapping
# Wait until the trajectory reaches the last scan for which IMU data is available.
python3 - <<'PY'
import time
from pathlib import Path
import rosbag
import rospy

with rosbag.Bag("/input.bag") as bag:
    start = rospy.Time.from_sec(bag.get_end_time() - 2)
    topics = [rospy.get_param("/common/lid_topic"), rospy.get_param("/common/imu_topic")]
    scans, imu = [], 0.0
    for topic, msg, _ in bag.read_messages(topics=topics, start_time=start):
        stamp = msg.header.stamp.to_sec()
        if topic == topics[0]:
            scans.append((stamp, stamp + max(p.offset_time for p in msg.points) * 1e-9))
        else:
            imu = stamp
    first, last = max(pair for pair in scans if pair[1] <= imu)
target = (first + last) / 2
if rospy.get_param("/common/img_en"):
    # The final visual update may precede the final lidar scan by one camera period.
    target -= 0.15
pose = Path("/root/lamm_ws/src/FAST-LIVO2/Log/result/cccp_job.txt")
for _ in range(300):
    if pose.is_file():
        rows = pose.read_text().splitlines()
        if rows and rows[-1].split() and float(rows[-1].split()[0]) >= target:
            print("trajectory reached", rows[-1].split()[0], "required", target, flush=True)
            break
    time.sleep(1)
else:
    raise RuntimeError("trajectory did not reach end of synchronized input")
PY
rosnode kill /laserMapping
out=/root/lamm_ws/src/FAST-LIVO2/Log/PCD
for i in {1..300}; do
    ! pgrep -f '^/root/lamm_ws/devel/lib/fast_livo/fastlivo_mapping' >/dev/null && [[ -s "$out/$cloud" ]] && break
    sleep 1
done
! pgrep -f '^/root/lamm_ws/devel/lib/fast_livo/fastlivo_mapping' >/dev/null || exit 1
test -s "$out/$cloud"
kill -INT "$launcher" || true
wait "$launcher" || true
