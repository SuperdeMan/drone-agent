"""Check the browser-to-SSH boundary without flight hardware or an external service.

不依赖飞行硬件或外部服务，检查浏览器到 SSH 的边界。
"""

import copy
import http.client
import json
import threading

import pytest

from drone_agent.console.server import Bridge, ConsoleHTTPServer

RUN = "20260921T120000Z-0123abcd"


@pytest.fixture
def console():
    calls = []

    def request(value):
        calls.append(value)
        if value["action"] == "live_status":
            return {"job": None, "source_sha": "a" * 40, "fresh": False, "allowed_actions": []}
        return {"status": "submitted", "run_id": value["run_id"]}

    bridge = Bridge(request, lambda _job: {})
    server = ConsoleHTTPServer(0, bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, calls
    server.shutdown()
    server.server_close()
    thread.join(2)


def call(server, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    defaults = {"Content-Type": "application/json", "Origin": server.origin, "X-Console-Nonce": server.nonce}
    defaults.update(headers or {})
    connection.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, defaults)
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def test_open_refresh_and_script_reads_never_start_a_flight(console):
    server, calls = console
    for path in ("/", "/live.js", "/api/state", "/api/state"):
        status, _, headers = call(server, path)
        assert status == 200 and headers["Cache-Control"] == "no-store"
    assert all(c["action"] == "live_status" for c in calls)
    assert server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.invalid"}, {"Origin": "null"}, {"Origin": ""},
    {"X-Console-Nonce": "wrong"}, {"Host": "attacker.invalid"},
])
def test_cross_origin_or_missing_session_cannot_start(console, headers):
    server, calls = console
    assert call(server, "/api/start", {"run_id": RUN, "seed": 7}, headers)[0] == 403
    assert calls == []


def test_fixed_start_preserves_idempotency_identity(console):
    server, calls = console
    body = {"run_id": RUN, "seed": 7}
    assert call(server, "/api/start", body)[0] == 202
    assert call(server, "/api/start", body)[0] == 202
    assert calls == [{"action": "live_start", **body}] * 2
    assert call(server, "/api/start", body | {"waypoint": [99, 99, 99]})[0] == 409
    assert len(calls) == 2


def test_operation_preserves_observed_target_and_never_accepts_arbitrary_paths(console):
    server, calls = console
    body = {"run_id": RUN, "request_id": "operator-123", "operation": "cancel", "mission_id": "mission-a", "step_id": "land"}
    assert call(server, "/api/operate", body)[0] == 202
    assert calls[-1] == {"action": "live_operate", **body}
    assert call(server, "/api/operate", body | {"path": "../another-project"})[0] == 409
    assert call(server, "/evidence/../../private")[0] == 404


def test_transport_failure_never_reuses_a_fresh_cached_snapshot(console):
    server, _ = console
    server.bridge.state()
    server.bridge.cached_at = 0

    def unavailable(_request):
        raise RuntimeError("cloud link unavailable")

    server.bridge.request = unavailable
    status, data, _ = call(server, "/api/state")
    assert status == 503
    assert json.loads(data)["fresh"] is False


def test_camera_and_position_history_do_not_leak_between_runs():
    sample = {
        "job": {"run_id": RUN}, "camera": {"width": 1, "height": 1, "rgb": "AAEA"},
        "runtime": {"observation": {"pose": {"position": {"x": 1, "y": 2, "z": 3}}, "timestamp": "now", "sample_id": 1}},
    }
    bridge = Bridge(lambda _request: copy.deepcopy(sample), None)
    state = bridge.state()
    assert state["camera"]["png"].startswith("data:image/png;base64,")
    assert "rgb" not in state["camera"] and len(state["estimate"]) == 1
    sample["job"]["run_id"] = "20260921T120001Z-0123abcd"
    sample.pop("runtime")
    bridge.cached_at = 0
    assert bridge.state()["estimate"] == []
