"""Reject replayed/expired/unsupported commands through the actual local RPC.

通过真实本地 RPC 检查重放、过期与未支持命令的拒绝。
"""

import argparse
import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path

from drone_agent.contracts import ControlCommandEnvelope
from drone_agent.runtime.ipc import GuardianClient
from drone_agent.runtime.ledger import read_log


async def probe(kind, artifacts):
    rows = read_log(artifacts / "executive.jsonl")
    value = next(row["data"]["envelope"] for row in reversed(rows) if row["kind"] == "command_submitted")
    envelope = ControlCommandEnvelope.model_validate(value)
    original_key = envelope.key.model_copy(deep=True)
    if kind != "duplicate":
        envelope.key.command_id += "-probe"
        envelope.command_seq += 100
    if kind == "stale_epoch":
        envelope.key.lease_epoch -= 1
    elif kind == "expired_intent":
        envelope.issued_at -= timedelta(seconds=30)
        envelope.valid_until -= timedelta(seconds=20)
    elif kind == "offboard_rejected":
        envelope.intent_kind = "trajectory_segment"
    client = GuardianClient("unix:/run/drone/guardian.sock")
    try:
        deadline = time.monotonic() + 5
        while (await client.reconcile(original_key))["receipt_status"] != "recorded":
            if time.monotonic() >= deadline:
                raise RuntimeError("original dispatch never acquired a recorded receipt")
            await asyncio.sleep(0.1)
        result = await client.submit(envelope)
        expected = kind == "duplicate"
        result["passed"] = bool(result.get("accepted")) == expected
        (artifacts / "probe.json").write_text(json.dumps(result))
        print(json.dumps(result))
        return result
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind")
    parser.add_argument("--artifacts", type=Path, default=Path("/artifacts"))
    args = parser.parse_args()
    result = asyncio.run(probe(args.kind, args.artifacts))
    raise SystemExit(0 if result["passed"] else 1)
