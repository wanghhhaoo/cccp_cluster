cmake_minimum_required(VERSION 3.8)
project(cccp_task_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ErrorCodes.msg"
  "msg/TaskStatus.msg"
  "msg/TaskReport.msg"
  "msg/TaskResult.msg"
  "srv/CreateTask.srv"
  "srv/DestroyTask.srv"
  "srv/QueryTask.srv"
  "srv/StartTask.srv"
  "srv/StopTask.srv"
  DEPENDENCIES builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
