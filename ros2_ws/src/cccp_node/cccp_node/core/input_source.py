"""Task input sources (CONVENTIONS 4.4): where a task's data comes from, independent of its type.

CreateTask.input holds a source address; requests carry the address, never the data:

  ""                            the executing node's default camera (behaviour before addresses existed)
  airsim://<vehicle>/<camera>   AirSim camera: SHARED, every node can fetch it over RPC
  ros://<node>/<topic>          a topic on one node (a real sensor): NODE-bound, only that node has it

plan_input() decides, for one participant of a task, which topic to subscribe
and whether this node must fetch from AirSim itself. Large data only ever
crosses the network as a topic (published once, compressed), never inside a
service request.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .naming import ros_namespace

LOCAL = "local"
AIRSIM = "airsim"
ROS = "ros"

FETCH_EACH = "each"     # every participant fetches the AirSim camera itself
FETCH_RELAY = "relay"   # only the task primary fetches; helpers receive its compressed stream

COMPRESSED_SUFFIX = "/compressed"

_AIRSIM = re.compile(r"^airsim://([^/]*)/([^/]+)$")
_ROS = re.compile(r"^ros://([^/]+)/(.+)$")
_TOPIC = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(/[A-Za-z][A-Za-z0-9_]*)*$")


@dataclass(frozen=True)
class InputSource:
    uri: str
    kind: str
    owner: str = ""      # ros: node holding the data
    vehicle: str = ""    # airsim
    camera: str = ""     # airsim
    topic: str = ""      # ros: topic relative to the owner's namespace

    @property
    def shared(self) -> bool:
        """Any node can read it directly, so tasks can move without changing their input."""
        return self.kind == AIRSIM


@dataclass(frozen=True)
class InputPlan:
    topic: str                    # relative (this node) or absolute (another node) name to subscribe
    compressed: bool = False      # sensor_msgs/CompressedImage instead of sensor_msgs/Image
    fetch: tuple[str, str] | None = None   # (vehicle, camera): run an AirSim feed publishing `topic`


def parse_input(uri: str) -> InputSource:
    """Raise ValueError for addresses this platform cannot serve."""
    uri = (uri or "").strip()
    if uri in ("", LOCAL):
        return InputSource(uri, LOCAL)
    match = _AIRSIM.match(uri)
    if match:
        return InputSource(uri, AIRSIM, vehicle=match.group(1), camera=match.group(2))
    match = _ROS.match(uri)
    if match:
        topic = match.group(2).strip("/")
        if not _TOPIC.match(topic):
            raise ValueError("ros input topic %r is not a valid relative ROS name" % topic)
        return InputSource(uri, ROS, owner=match.group(1), topic=topic)
    raise ValueError("input must be empty, airsim://<vehicle>/<camera> or ros://<node>/<topic>, got %r" % uri)


def airsim_topic(vehicle: str, camera: str) -> str:
    """Local topic an AirSim feed publishes on, e.g. input/airsim/uav_5/cam_0."""
    camera_token = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", camera))
    return "input/airsim/%s/cam_%s" % (ros_namespace(vehicle or "default")[1:], camera_token)


def plan_input(source: InputSource, *, node_name: str, default_topic: str, image_topic: str = "",
               role: str = "primary", primary_node: str = "", airsim_mode: str = FETCH_EACH) -> InputPlan:
    if source.kind == LOCAL:
        return InputPlan(image_topic or default_topic)
    if source.kind == ROS:
        if source.owner == node_name:
            return InputPlan(source.topic)
        return InputPlan("%s/%s%s" % (ros_namespace(source.owner), source.topic, COMPRESSED_SUFFIX), compressed=True)
    topic = airsim_topic(source.vehicle, source.camera)
    if airsim_mode == FETCH_RELAY and role != "primary" and primary_node and primary_node != node_name:
        return InputPlan("%s/%s%s" % (ros_namespace(primary_node), topic, COMPRESSED_SUFFIX), compressed=True)
    return InputPlan(topic, fetch=(source.vehicle, source.camera))
