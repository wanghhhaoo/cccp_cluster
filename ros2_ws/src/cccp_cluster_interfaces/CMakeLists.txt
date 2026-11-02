cmake_minimum_required(VERSION 3.8)
project(cccp_cluster_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/TaskBrief.msg"
  "msg/LinkQuality.msg"
  "msg/NodeLoad.msg"
  "msg/TaskState.msg"
  "msg/Tombstone.msg"
  "msg/NodeStatus.msg"
  "msg/TaskList.msg"
  "msg/ExecutorStatus.msg"
  "msg/Command.msg"
  "msg/ExecEvent.msg"
  "msg/Request.msg"
  "msg/Reply.msg"
  DEPENDENCIES builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
