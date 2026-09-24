# da_egress_ext: guardian-authorized PX4 external mode (D039). The drone.autonomy.v1 proto is compiled from the
# repository's proto/ directory (DA_PROTO_ROOT) with the system protoc, so both sides share one wire definition.
# da_egress_ext：由 guardian 授权的 PX4 外部模式（D039）。drone.autonomy.v1 proto 用系统 protoc 从仓库的 proto/ 目录
#（DA_PROTO_ROOT）编译，两侧共用同一份线路定义。
cmake_minimum_required(VERSION 3.22)
project(da_egress_ext CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra -Wpedantic)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(px4_msgs REQUIRED)
find_package(px4_ros2_cpp REQUIRED)
find_package(Eigen3 REQUIRED)
find_package(Protobuf REQUIRED)

if(NOT DA_PROTO_ROOT)
  message(FATAL_ERROR "set DA_PROTO_ROOT to the repository proto/ directory")
endif()

set(AUTONOMY_PROTO ${DA_PROTO_ROOT}/drone/autonomy/v1/autonomy.proto)
set(GENERATED ${CMAKE_CURRENT_BINARY_DIR}/generated)
file(MAKE_DIRECTORY ${GENERATED})
add_custom_command(
  OUTPUT ${GENERATED}/drone/autonomy/v1/autonomy.pb.cc ${GENERATED}/drone/autonomy/v1/autonomy.pb.h
  COMMAND ${Protobuf_PROTOC_EXECUTABLE} --cpp_out=${GENERATED} -I${DA_PROTO_ROOT} -I${Protobuf_INCLUDE_DIRS}
          ${AUTONOMY_PROTO}
  DEPENDS ${AUTONOMY_PROTO}
)

add_executable(egress_node src/egress_node.cpp ${GENERATED}/drone/autonomy/v1/autonomy.pb.cc)
target_include_directories(egress_node PRIVATE ${GENERATED})
target_link_libraries(egress_node px4_ros2_cpp::px4_ros2_cpp Eigen3::Eigen protobuf::libprotobuf)
ament_target_dependencies(egress_node rclcpp px4_msgs)

install(TARGETS egress_node DESTINATION lib/${PROJECT_NAME})
ament_package()
