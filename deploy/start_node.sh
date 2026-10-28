#!/bin/bash
# Starts the three processes of one cluster node (CONVENTIONS 6.1):
#   control     cccp_node control_node: Raft replica, leader control plane, gateway, NodeStatus
#   data plane  cccp_task_executor data_plane: task executor + PKCI + PKCT services
#   camera      cccp_task_executor airsim_camera: only when AIRSIM_HOST is set
# Adapted from CCCP start_edge_agent.sh.
set -e

if [ -z "${AMENT_TRACE_SETUP_FILES+x}" ]; then
	unset AMENT_TRACE_SETUP_FILES
fi
source /opt/ros/humble/setup.bash
if [ -f /ros2_ws/install/setup.bash ]; then
	source /ros2_ws/install/setup.bash
fi

NODE_NAME=${NODE_NAME:?NODE_NAME must be set and listed in cluster.yaml}
CLUSTER_CONFIG=${CLUSTER_CONFIG:-/etc/cccp/cluster.yaml}
CCCP_DATA_DIR=${CCCP_DATA_DIR:-/var/lib/cccp}
UE_HTTP_PORT=${UE_HTTP_PORT:-18090}
LOG_LEVEL=${LOG_LEVEL:-INFO}
export RCUTILS_LOGGING_MIN_SEVERITY=$LOG_LEVEL
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}

ENABLE_CONTROL=${ENABLE_CONTROL:-true}
ENABLE_DATA_PLANE=${ENABLE_DATA_PLANE:-true}
export ENABLE_PKCI=${ENABLE_PKCI:-true}
export ENABLE_PKCT=${ENABLE_PKCT:-true}
COLLAB_DEVICE=${COLLAB_DEVICE:-cuda:0}
MAX_TRACKERS=${MAX_TRACKERS:-4}
IMAGE_TOPIC=${IMAGE_TOPIC:-camera/image}
AIRSIM_HOST=${AIRSIM_HOST:-}
AIRSIM_PORT=${AIRSIM_PORT:-41451}
AIRSIM_VEHICLE=${AIRSIM_VEHICLE:-}
AIRSIM_CAMERA=${AIRSIM_CAMERA:-0}
AIRSIM_FPS=${AIRSIM_FPS:-10}
AIRSIM_RAW_ENCODING=${AIRSIM_RAW_ENCODING:-bgr8}
ENABLE_AIRSIM_CAMERA=${ENABLE_AIRSIM_CAMERA:-$([ -n "$AIRSIM_HOST" ] && echo true || echo false)}

# Namespace rule shared with cccp_node/core/naming.py: invalid chars -> '_',
# squeeze '_', prefix 'n' before a leading digit.
ns_token=$(printf '%s' "$NODE_NAME" | sed -E 's/[^A-Za-z0-9_]/_/g; s/_+/_/g')
case "$ns_token" in
	'' | [0-9]*) ns_token="n${ns_token}" ;;
esac
NS_ARGS=(-r "__ns:=/${ns_token}")

echo "[start_node] NODE_NAME=$NODE_NAME ns=/${ns_token} CLUSTER_CONFIG=$CLUSTER_CONFIG DATA_DIR=$CCCP_DATA_DIR"
echo "[start_node] ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-N/A} RMW=$RMW_IMPLEMENTATION UE_HTTP_PORT=$UE_HTTP_PORT DEVICE=$COLLAB_DEVICE"

if [ "$ENABLE_DATA_PLANE" = "true" ] && { [ "$ENABLE_PKCI" = "true" ] || [ "$ENABLE_PKCT" = "true" ]; }; then
	python3 -c 'import torch, torchvision, numpy, PIL' || {
		echo "[start_node] Missing inference runtime: install the JetPack-matched NVIDIA torch/torchvision wheels, numpy and pillow." >&2
		exit 1
	}
fi

pids=()

if [ "$ENABLE_CONTROL" = "true" ]; then
	test -r "$CLUSTER_CONFIG" || { echo "[start_node] $CLUSTER_CONFIG not readable" >&2; exit 1; }
	mkdir -p "$CCCP_DATA_DIR"
	setsid ros2 run cccp_node control_node --ros-args --log-level "$LOG_LEVEL" "${NS_ARGS[@]}" \
		-p "node_name:=${NODE_NAME}" \
		-p "cluster_config:=${CLUSTER_CONFIG}" \
		-p "data_dir:=${CCCP_DATA_DIR}" \
		-p "ue_http_port:=${UE_HTTP_PORT}" &
	pids+=("$!")
fi

if [ "$ENABLE_DATA_PLANE" = "true" ]; then
	# Unbounded torch CPU threads made inference ~5x slower on a 144-core host (docs/HANDOFF.md).
	# airsim:// task inputs are fetched on demand when AIRSIM_HOST is set (CONVENTIONS 4.4).
	# AIRSIM_FETCH_MODE=each: every participant queries AirSim; relay: only the task primary does.
	case "$AIRSIM_FPS" in *.*) AIRSIM_FPS_PARAM="$AIRSIM_FPS" ;; *) AIRSIM_FPS_PARAM="${AIRSIM_FPS}.0" ;; esac
	OMP_NUM_THREADS=${INFERENCE_THREADS:-4} setsid ros2 run cccp_task_executor data_plane --ros-args --log-level "$LOG_LEVEL" "${NS_ARGS[@]}" \
		-p "node_name:=${NODE_NAME}" \
		-p "image_topic:=${IMAGE_TOPIC}" \
		-p "airsim_host:=${AIRSIM_HOST}" \
		-p "airsim_port:=${AIRSIM_PORT}" \
		-p "airsim_fps:=${AIRSIM_FPS_PARAM}" \
		-p "airsim_raw_encoding:=${AIRSIM_RAW_ENCODING}" \
		-p "airsim_fetch_mode:=${AIRSIM_FETCH_MODE:-each}" \
		-p "device:=${COLLAB_DEVICE}" \
		-p "max_trackers:=${MAX_TRACKERS}" &
	pids+=("$!")
fi

if [ "$ENABLE_AIRSIM_CAMERA" = "true" ]; then
	if [ -z "$AIRSIM_HOST" ]; then
		echo "[start_node] ENABLE_AIRSIM_CAMERA=true requires AIRSIM_HOST" >&2
		exit 1
	fi
	case "$AIRSIM_FPS" in *.*) AIRSIM_FPS_PARAM="$AIRSIM_FPS" ;; *) AIRSIM_FPS_PARAM="${AIRSIM_FPS}.0" ;; esac
	setsid ros2 run cccp_task_executor airsim_camera --ros-args --log-level "$LOG_LEVEL" "${NS_ARGS[@]}" \
		-p "airsim_host:=${AIRSIM_HOST}" \
		-p "airsim_port:=${AIRSIM_PORT}" \
		-p "vehicle_name:=${AIRSIM_VEHICLE}" \
		-p "camera_name:=${AIRSIM_CAMERA}" \
		-p "fps:=${AIRSIM_FPS_PARAM}" \
		-p "raw_encoding:=${AIRSIM_RAW_ENCODING}" \
		-p "output_topic:=${IMAGE_TOPIC}" &
	pids+=("$!")
fi

if [ "${#pids[@]}" -eq 0 ]; then
	echo "[start_node] No processes enabled" >&2
	exit 1
fi

# Each process runs in its own process group (setsid) so stopping the group
# also stops the node that `ros2 run` spawned, not just the wrapper.
cleanup() {
	trap - EXIT INT TERM
	for pid in "${pids[@]}"; do
		kill -TERM -- "-$pid" 2>/dev/null || true
	done
	wait "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait -n "${pids[@]}"
