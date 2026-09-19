"""Reconstruct mission events from MCAP and independently rejudge the recorded run.

从 MCAP 重建任务事件，并对记录的运行重新执行独立裁判。
"""

import argparse
import json
from pathlib import Path

from drone_agent.eval.judge import judge
from drone_agent.runtime.ledger import canonical, content_hash
from drone_agent.runtime.recording import replay


def rejudge(run: Path, root: Path):
    events = [data for topic, data in replay(run / "aircraft/executive.mcap") if topic == "mission/events"]
    previous = "0" * 64
    for index, event in enumerate(events):
        payload = {key: value for key, value in event.items() if key != "sha256"}
        if event["seq"] != index or event["previous"] != previous or event["sha256"] != content_hash(payload):
            raise ValueError("replayed event integrity failed")
        previous = event["sha256"]
    return judge(run, root, replayed_events=events)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--output", type=Path, default=Path("/output/replay.json"))
    args = parser.parse_args()
    result = rejudge(args.run, args.root)
    args.output.write_bytes(canonical(result))
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
