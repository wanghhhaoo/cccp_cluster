# x86_64 node image. Build from the repository root:
#   docker build -f deploy/Dockerfile -t cccp-node:p1-$(git rev-parse --short HEAD) .
# Run (host network so DDS and Raft reach the other nodes):
#   docker run --network host -e NODE_NAME=edge-01 \
#     -v /etc/cccp/cluster.yaml:/etc/cccp/cluster.yaml:ro -v cccp-raft:/var/lib/cccp cccp-node:p1-...
# Adapted from CCCP edge/Dockerfile_build. The C++ monitor_node is no longer built:
# resource reporting moved into the control process.
FROM ros:humble

WORKDIR /ros2_ws
COPY ros2_ws/src ./src
COPY deploy/start_node.sh ./start_node.sh
RUN chmod +x /ros2_ws/start_node.sh

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		python3-colcon-common-extensions \
		python3-pip \
		python3-numpy \
		python3-pil \
		python3-yaml \
		python3-msgpack \
		python3-tornado \
		python3-pytest \
		ros-humble-cv-bridge \
		ros-humble-rmw-cyclonedds-cpp \
		libgl1 \
		libglib2.0-0 \
		libopenblas-dev \
		ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

# Raft (CONVENTIONS 3.1) and the AirSim RPC client. --no-deps keeps apt's numpy,
# msgpack and tornado so cv_bridge stays compatible.
RUN pip3 install --no-cache-dir pysyncobj==0.3.17 \
	&& pip3 install --no-cache-dir --no-deps cosysairsim==3.3.0 rpc-msgpack==0.6

ENV ROS_DOMAIN_ID=10
RUN bash -lc "source /opt/ros/humble/setup.bash && colcon build --merge-install \
	--packages-up-to cccp_node cccp_task_executor collab_inference"

# PyTorch after colcon: recent torch pulls setuptools>=77, which breaks ament's
# Python build on Ubuntu 22.04. CPU wheels by default; for an NVIDIA GPU build with
#   --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126  and run with COLLAB_DEVICE=cuda:0
ARG INSTALL_PYTORCH=true
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN if [ "$INSTALL_PYTORCH" = "true" ]; then \
		pip3 install --no-cache-dir ${TORCH_INDEX_URL:+--index-url "$TORCH_INDEX_URL"} torch torchvision; \
	fi

ENV CLUSTER_CONFIG=/etc/cccp/cluster.yaml
ENV CCCP_DATA_DIR=/var/lib/cccp
ENV UE_HTTP_PORT=18090
ENV COLLAB_DEVICE=cpu
ENV MAX_TRACKERS=4
ENV IMAGE_TOPIC=camera/image
VOLUME ["/var/lib/cccp"]

CMD ["/ros2_ws/start_node.sh"]
