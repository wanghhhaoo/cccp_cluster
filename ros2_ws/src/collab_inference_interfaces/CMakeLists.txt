cmake_minimum_required(VERSION 3.8)
project(collab_inference_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(sensor_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/EncodedPayload.msg"
  "srv/Classify.srv"
  "srv/FuseClassification.srv"
  "srv/InitTracker.srv"
  "srv/Track.srv"
  "srv/ElectPrimary.srv"
  "srv/ResetTracker.srv"
  DEPENDENCIES sensor_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
