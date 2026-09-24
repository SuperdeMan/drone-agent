"""Simulated camera driver: a fixed allowlist of Gazebo camera topics republished as ROS 2 images (D041).

It stands in for the camera drivers of a real aircraft, so the perception and local-map nodes consume ordinary
`sensor_msgs/Image` topics and never touch Gazebo. Only the two camera streams below are relayed; poses, contacts and
every other simulator topic are not, so no ground truth can reach the autonomy side through this process.

仿真相机驱动：把固定清单内的 Gazebo 相机话题转发为 ROS 2 图像（D041）。

它替代真实飞行器上的相机驱动，使感知与局部地图节点只消费普通的 `sensor_msgs/Image` 话题，从不接触 Gazebo。只转发
下面两路相机流；位姿、接触及其他仿真话题一概不转发，因此真值无法经本进程到达自主层。
"""

import os
import signal
import threading

import rclpy
from gz.msgs10.image_pb2 import Image as GzImage
from gz.transport13 import Node as GzNode
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

# Gazebo topic -> (ROS topic, encoding, bytes per pixel, expected Gazebo pixel format).
# Gazebo 话题 -> （ROS 话题、编码、每像素字节数、期望的 Gazebo 像素格式）。
RELAYS = {
    "/drone/cam_0/image": ("/uav_01/cam_0/image", "rgb8", 3, "RGB_INT8"),
    "/drone/depth_0/depth_image": ("/uav_01/depth_0/image", "32FC1", 4, "R_FLOAT32"),
}


def main():
    rclpy.init()
    node = rclpy.create_node("da_sim_camera_relay")
    publishers = {name: node.create_publisher(Image, spec[0], qos_profile_sensor_data) for name, spec in RELAYS.items()}
    gz = GzNode()

    def relay(name):
        ros_topic, encoding, depth, expected = RELAYS[name]

        def callback(message):
            fmt = message.DESCRIPTOR.fields_by_name["pixel_format_type"].enum_type.values_by_number[
                message.pixel_format_type].name
            if fmt != expected or message.step != message.width * depth:
                return
            image = Image()
            image.header.stamp.sec, image.header.stamp.nanosec = message.header.stamp.sec, message.header.stamp.nsec
            image.header.frame_id = ros_topic.split("/")[2]
            image.height, image.width, image.encoding = message.height, message.width, encoding
            image.is_bigendian, image.step, image.data = 0, message.step, bytes(message.data)
            publishers[name].publish(image)

        return callback

    for name in RELAYS:
        assert gz.subscribe(GzImage, name, relay(name)), f"cannot subscribe {name}"
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    while not stopped.wait(0.5):
        rclpy.spin_once(node, timeout_sec=0)
    # Stop the Gazebo callbacks first, then leave without interpreter finalizers: gz-transport's callback threads
    # would otherwise drop Python references without the GIL while the interpreter shuts down.
    # 先停掉 Gazebo 回调，再跳过解释器终结器退出：否则 gz-transport 的回调线程会在解释器关闭期间不持 GIL 释放
    # Python 引用。
    for name in RELAYS:
        gz.unsubscribe(name)
    node.destroy_node()
    rclpy.try_shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
