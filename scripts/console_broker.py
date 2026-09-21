"""Local capability broker for the private web console; no arbitrary cloud commands.

私有网页控制台的本机能力代理；不接受任意云端命令。
"""

from __future__ import annotations

import json
import os
import runpy
import socket
import socketserver
import stat
import struct
import threading
from pathlib import Path

HELPERS = runpy.run_path(str(Path(__file__).with_name("remote_dev_stack.py")))
MAX_REQUEST = 8192
FIELDS = {
    "live_status": {"action"},
    "live_start": {"action", "run_id", "seed"},
    "live_operate": {"action", "run_id", "request_id", "operation", "mission_id", "step_id"},
}


def dispatch(root: Path, envelope: dict, *, peer_uid: int, owner_uid: int) -> dict:
    if peer_uid != owner_uid:
        raise ValueError("broker peer identity rejected")
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "request"} or envelope["schema_version"] != "0.1.0":
        raise ValueError("unsupported broker envelope")
    request = envelope["request"]
    if not isinstance(request, dict) or request.get("action") not in FIELDS or set(request) != FIELDS[request["action"]]:
        raise ValueError("broker method or fields rejected")
    deployment = HELPERS["current"](root)
    live = runpy.run_path(str(deployment / "source/scripts/remote_live.py"))
    if request["action"] == "live_status":
        return live["snapshot"](root, deployment)
    return live["start" if request["action"] == "live_start" else "operate"](root, deployment, request)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                raise ValueError("invalid broker frame")
            result = dispatch(self.server.root, json.loads(raw), peer_uid=uid, owner_uid=os.getuid())
            response = {"ok": True, "result": result}
        except Exception as error:
            response = {"ok": False, "error": str(error)}
        self.wfile.write(json.dumps(response).encode() + b"\n")


class Broker(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = getattr(socket, "AF_UNIX", socket.AF_INET)
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, root: Path, endpoint: Path):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("the cloud broker requires Linux Unix peer credentials")
        self.root = root
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(str(endpoint), Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def remove_stale_socket(endpoint: Path, owner_uid: int):
    if not endpoint.exists():
        return
    details = endpoint.lstat()
    if not stat.S_ISSOCK(details.st_mode) or details.st_uid != owner_uid:
        raise ValueError("refusing to replace a non-owned socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        try:
            probe.connect(str(endpoint))
        except ConnectionRefusedError:
            endpoint.unlink()
        else:
            raise ValueError("broker socket is already active")


def main():
    import fcntl

    root = HELPERS["workspace"]()
    directory = root / "console/ipc"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("invalid broker directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with (directory / "broker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        endpoint = directory / "broker.sock"
        remove_stale_socket(endpoint, os.getuid())
        with Broker(root, endpoint) as server:
            os.chmod(endpoint, 0o600)
            server.serve_forever()


if __name__ == "__main__":
    main()
