"""Event-detection node (WP-M3-15, D041, D044): downward RGB -> CLIP zero-shot scene class -> candidate BeliefFact.

The ROS callback only keeps the newest frame; a worker thread classifies at most once per period (1 Hz by default,
back to back with `--period-s 0` for the full-inference load tier of D040) and drops frames it had no time for, so the
node never queues work and never blocks anything else. Each result goes to the executive as a model-sourced belief
fact with a two-second validity, which the executive journals as a candidate; a class that declares a replan trigger
sets the flag when it wins with at least the configured probability. The node has no path to the guardian or the
flight controller, refuses to start on a vision model whose SHA-256 differs from the one the prompt table was built
for, and writes one evidence row per inference with its latency.

事件检测节点（WP-M3-15，D041，D044）：下视 RGB -> CLIP 零样本场景类别 -> 候选 BeliefFact。

ROS 回调只保留最新一帧；工作线程每个周期最多分类一次（默认 1 Hz；`--period-s 0` 时连续推理，对应 D040 的推理满载档），
来不及处理的帧直接丢弃，因此节点从不积压、也不阻塞任何其他部分。每个结果以模型来源、两秒有效期的信念事实发给
executive，由其记为候选；声明了重规划触发的类别以不低于配置概率胜出时置位触发标志。节点没有通往 guardian 或飞控的
路径；视觉模型的 SHA-256 与提示表构建时不同则拒绝启动；每次推理写一行带延迟的证据。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rclpy
import yaml
from da_common.ipc import SCHEMA_VERSION, FrameClient, pb
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from da_edge_inference.build_prompts import sha256
from da_edge_inference.clip import class_embeddings, classify, decide, preprocess


class Classifier:
    """The prompt table and the pinned vision encoder. / 提示表与固定的视觉编码器。"""

    def __init__(self, table: dict, model: Path, *, threads: int):
        import onnxruntime as ort

        if sha256(model) != table["model"]["vision"]["sha256"]:
            raise SystemExit("the vision model does not match the pinned SHA-256 of the prompt table")
        self.table = table
        self.labels, self.classes = class_embeddings({k: v["embeddings"] for k, v in table["classes"].items()})
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.outputs = [output.name for output in self.session.get_outputs()]
        revision = table["model"]["revision"][:7]
        self.version = f"clip-vit-b32-q8@{revision}+{table['prompt_set']}"

    def __call__(self, rgb: np.ndarray) -> dict[str, float]:
        result = dict(zip(self.outputs, self.session.run(None, {self.input: preprocess(rgb)}), strict=True))
        return classify(result["image_embeds"][0], self.labels, self.classes)


class EdgeInferenceNode(Node):
    def __init__(self, args, classifier: Classifier):
        super().__init__("da_edge_inference")
        self.args, self.classifier = args, classifier
        scene = yaml.safe_load(Path(args.scene).read_text(encoding="utf-8"))
        self.frame = scene["frame"]
        runtime = classifier.table["runtime"]
        self.validity = timedelta(seconds=float(runtime["fact_validity_s"]))
        self.trigger_probability = float(runtime["trigger_probability"])
        self.lock = threading.Lock()
        self.latest = None
        self.dropped = 0
        self.stopped = threading.Event()
        self.log = Path(args.evidence).open("a", buffering=1)
        self.link = FrameClient(args.socket, name="edge").start()
        self.create_subscription(Image, args.rgb_topic, self.on_image, qos_profile_sensor_data)
        self.worker = threading.Thread(target=self.run, name="edge-inference", daemon=True)
        self.worker.start()

    def on_image(self, message):
        if message.encoding != "rgb8" or message.step != message.width * 3:
            return
        frame = (bytes(message.data), message.height, message.width, time.monotonic(), datetime.now(timezone.utc))
        with self.lock:
            if self.latest is not None:
                self.dropped += 1
            self.latest = frame

    def run(self):
        last = 0.0
        while not self.stopped.is_set():
            wait = self.args.period_s - (time.monotonic() - last)
            if wait > 0 and self.stopped.wait(wait):
                return
            with self.lock:
                frame, self.latest = self.latest, None
                dropped, self.dropped = self.dropped, 0
            if frame is None:
                self.stopped.wait(0.02)
                continue
            last = time.monotonic()
            self.infer(frame, dropped)

    def infer(self, frame, dropped: int) -> None:
        data, height, width, received, captured = frame
        start = time.monotonic()
        rgb = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
        probabilities = self.classifier(rgb)
        inference_ms = (time.monotonic() - start) * 1000
        classes = self.classifier.table["classes"]
        label, probability, trigger = decide(probabilities, classes, self.trigger_probability)
        spec = classes[label]
        fact = {
            "fact_id": uuid.uuid4().hex, "subject": f"{self.args.robot_id}.cam_0", "predicate": "scene_event",
            "value": {"label": label, "relevant": spec["relevant"], "prompt_set": self.classifier.table["prompt_set"],
                      "probabilities": {k: round(v, 4) for k, v in probabilities.items()}},
            "frame": {"frame_id": self.frame["frame_id"], "map_version": self.frame["map_version"]},
            "timestamp": captured.isoformat(), "valid_until": (captured + self.validity).isoformat(),
            "source": "model", "source_version": self.classifier.version, "confidence": round(probability, 4),
            "world_kind": "belief",
        }
        message = pb.AutonomyFrame()
        body = message.belief_fact
        body.schema_version = SCHEMA_VERSION
        body.producer_id = "da_edge_inference"
        body.fact = json.dumps(fact).encode()
        latency_ms = (time.monotonic() - received) * 1000
        body.latency_ms = latency_ms
        body.replan_trigger = trigger
        sent = self.link.send(message)
        self.log.write(json.dumps({
            "wall_time": time.time(), "captured": captured.isoformat(), "inference_ms": round(inference_ms, 2),
            "latency_ms": round(latency_ms, 2), "label": label,
            "probability": round(probability, 4), "replan_trigger": trigger, "sent": sent, "dropped_frames": dropped,
        }) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/belief/belief.sock")
    parser.add_argument("--scene", default="/workspace/configs/scenarios/m3_campus_v3.yaml")
    parser.add_argument("--prompts", default="/opt/da_edge/prompts.json")
    parser.add_argument("--model", default="/opt/da_edge/model/vision_model_quantized.onnx")
    parser.add_argument("--rgb-topic", default="/uav_01/cam_0/image")
    parser.add_argument("--robot-id", default="uav_01")
    parser.add_argument("--period-s", type=float, default=1.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--evidence", default="/artifacts/edge_inference.jsonl")
    args, ros_args = parser.parse_known_args()
    if args.period_s < 0:
        raise SystemExit("--period-s must be zero (back to back) or positive")
    classifier = Classifier(json.loads(Path(args.prompts).read_text(encoding="utf-8")), Path(args.model),
                            threads=args.threads)
    rclpy.init(args=ros_args)
    node = EdgeInferenceNode(args, classifier)
    try:
        rclpy.spin(node)
    finally:
        node.stopped.set()
        node.link.close()
        node.log.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
