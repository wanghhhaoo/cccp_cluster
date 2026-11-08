#!/bin/bash
# Runs this board's cluster node as a container (deploy/README.md). Replaces a running one.
#   deploy/run_node.sh [env file]      default: deploy/node.env (see deploy/node.env.example)
set -euo pipefail

env_file=${1:-$(dirname "$(readlink -f "$0")")/node.env}
if [ ! -f "$env_file" ]; then
	echo "run_node: $env_file not found (copy deploy/node.env.example)" >&2
	exit 1
fi
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

: "${NODE_NAME:?set NODE_NAME in $env_file}"
: "${IMAGE:?set IMAGE in $env_file}"
: "${CCCP_DDS_INTERFACE:?set CCCP_DDS_INTERFACE in $env_file}"
# Docker network: host on the boards. A bridge network runs several nodes on one machine (DESIGN 10.2); the
# interface is then the container's own (eth0), which start_node.sh checks inside.
network=${DOCKER_NETWORK:-host}
if [ "$network" = "host" ] && [ ! -e "/sys/class/net/$CCCP_DDS_INTERFACE" ]; then
	echo "run_node: no interface $CCCP_DDS_INTERFACE here; interfaces: $(ls /sys/class/net | tr '\n' ' ')" >&2
	exit 1
fi
case "${CCCP_DDS_MULTICAST:-default}" in
	default | true | spdp) ;;
	# Without a Peers list, discovery (SPDP) needs multicast: every node would be alone and lead itself.
	*) echo "run_node: CCCP_DDS_MULTICAST=$CCCP_DDS_MULTICAST: use default, true or spdp" >&2; exit 1 ;;
esac
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
	echo "run_node: image $IMAGE is not loaded (docker load -i ...)" >&2
	exit 1
fi

args=(--network "$network" --restart unless-stopped --env-file "$env_file"
	# Container logs live on the board's eMMC: bound them.
	--log-opt max-size=50m --log-opt max-file=5)
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
	args+=(--runtime nvidia)
elif [ "${COLLAB_DEVICE:-cpu}" != "cpu" ]; then
	echo "run_node: COLLAB_DEVICE=$COLLAB_DEVICE but Docker has no nvidia runtime" >&2
	exit 1
fi
if [ -n "${CCCP_JOB_STORE:-}" ]; then
	# The same path inside and outside: FASTLIVO hands it to the Docker daemon (docs/推理任务集成.md).
	args+=(-v "$CCCP_JOB_STORE:$CCCP_JOB_STORE")
fi

name="cccp-$NODE_NAME"
docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" "${args[@]}" "$IMAGE" >/dev/null
echo "run_node: started $name ($IMAGE) on $CCCP_DDS_INTERFACE (network $network), domain ${ROS_DOMAIN_ID:-default}"
echo "run_node: logs: docker logs -f $name"
