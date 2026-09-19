"""MCAP recording and deterministic record replay.

MCAP 录制与确定性记录回放。
"""

import json
import time
from pathlib import Path

from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from drone_agent.runtime.ledger import canonical


class Recorder:
    def __init__(self, path: Path):
        self.stream = path.open("wb")
        self.writer = Writer(self.stream, compression=CompressionType.NONE)
        self.writer.start(profile="drone-agent/v1")
        self.channels = {}

    def write(self, topic: str, data: dict):
        if topic not in self.channels:
            self.channels[topic] = self.writer.register_channel(topic, "json", 0)
        stamp = time.time_ns()
        self.writer.add_message(self.channels[topic], stamp, canonical(data), stamp)

    def close(self):
        self.writer.finish()
        self.stream.flush()
        self.stream.close()


def replay(path: Path):
    with path.open("rb") as stream:
        for _, channel, message in make_reader(stream).iter_messages():
            yield channel.topic, json.loads(message.data)
