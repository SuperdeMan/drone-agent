"""The Tailnet entry keeps the same operation boundary without granting web process privileges.

Tailnet 入口保留同一操作边界，不给网页进程管理权限。
"""

import copy
import hashlib
import http.client
import json
import runpy
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from drone_agent.console.application import ConsoleApplication, validate_origin
from drone_agent.console.cloud import ASGIConsole, CloudBackend
from drone_agent.console.server import Bridge

ROOT = Path(__file__).resolve().parents[2]
MANAGER = runpy.run_path(str(ROOT / "scripts/remote_console.py"))
BROKER = runpy.run_path(str(ROOT / "scripts/console_broker.py"))
ORIGIN = "https://example.test.ts.net:8447"
RUN = "20260922T000000Z-1234abcd"
DEPLOYMENT = "20260921T230000Z-1234abcd"


async def request(application, *, method="GET", path="/api/state", body=b"", extra_headers=(), disconnect=False):
    headers = [(b"host", b"example.test.ts.net:8447"), (b"origin", ORIGIN.encode()),
               (b"content-type", b"application/json"), (b"x-console-nonce", application.nonce.encode())]
    headers.extend(extra_headers)
    events = [{"type": "http.disconnect"} if disconnect else {"type": "http.request", "body": body}]
    sent = []

    async def receive():
        return events.pop(0)

    async def send(value):
        sent.append(value)

    await ASGIConsole(application)({"type": "http", "method": method, "path": path, "headers": headers}, receive, send)
    return sent


def application():
    calls = []

    def dispatch(value):
        calls.append(value)
        return {"source_sha": "a" * 40, "job": None, "status": "submitted"}

    return ConsoleApplication(Bridge(dispatch, None), ORIGIN, tailnet=True), calls


async def test_asgi_state_reads_and_start_share_the_original_bridge_boundary():
    app, calls = application()
    response = await request(app)
    assert response[0]["status"] == 200 and calls == [{"action": "live_status"}]
    response = await request(app, method="POST", path="/api/start", body=json.dumps({"run_id": RUN, "seed": 7}).encode())
    assert response[0]["status"] == 202
    assert calls[-1] == {"action": "live_start", "run_id": RUN, "seed": 7}


@pytest.mark.parametrize("headers", [[(b"origin", b"https://attacker.invalid")], [(b"host", b"evil.invalid")],
                                     [(b"x-console-nonce", b"duplicate")]])
async def test_asgi_rejects_duplicate_security_headers_before_dispatch(headers):
    app, calls = application()
    response = await request(app, method="POST", body=b"{}", extra_headers=headers)
    assert response[0]["status"] == 403 and calls == []


async def test_asgi_rejects_large_body_and_disconnect_without_starting():
    app, calls = application()
    assert (await request(app, method="POST", body=b"x" * 4097))[0]["status"] == 413
    assert await request(app, method="POST", disconnect=True) == []
    assert calls == []


@pytest.mark.parametrize("origin", ["http://example.test.ts.net:8447", "https://public.example.com", "https://user@example.test.ts.net", "https://example.test.ts.net/path", "https://example.test.ts.net/?token=test"])
def test_cloud_origin_requires_explicit_https_tailnet_authority(origin):
    with pytest.raises(ValueError):
        validate_origin(origin, tailnet=True)


@pytest.mark.parametrize("payload", [
    {"action": "deploy", "run_id": RUN}, {"action": "stop", "run_id": RUN},
    {"action": "live_status", "path": "/etc/passwd"},
    {"action": "live_start", "run_id": RUN, "seed": 7, "command": "anything"},
])
def test_broker_cannot_be_used_as_general_cloud_cli(tmp_path, payload):
    with pytest.raises(ValueError, match="method or fields"):
        BROKER["dispatch"](tmp_path, {"schema_version": "0.1.0", "request": payload}, peer_uid=1000, owner_uid=1000)


def test_broker_rejects_foreign_peer_before_reading_any_project_state(tmp_path):
    with pytest.raises(ValueError, match="identity"):
        BROKER["dispatch"](tmp_path, {"schema_version": "0.1.0", "request": {"action": "live_status"}}, peer_uid=1001, owner_uid=1000)
    assert list(tmp_path.iterdir()) == []


def test_broker_dispatches_only_the_fixed_live_method(tmp_path, monkeypatch):
    function = BROKER["dispatch"]
    calls = []
    deployment = tmp_path / "release"
    monkeypatch.setitem(function.__globals__["HELPERS"], "current", lambda root: deployment)
    monkeypatch.setattr(function.__globals__["runpy"], "run_path", lambda path: {
        "snapshot": lambda root, release: calls.append((root, release)) or {"job": None},
    })
    assert function(tmp_path, {"schema_version": "0.1.0", "request": {"action": "live_status"}}, peer_uid=1000, owner_uid=1000) == {"job": None}
    assert calls == [(tmp_path, deployment)]


def test_service_restart_preserves_detached_flight_and_cloud_web_has_no_docker_access():
    unit = MANAGER["unit_text"](Path("/home/ubuntu/drone-agent"), Path("/home/ubuntu/drone-agent/releases") / DEPLOYMENT, "ubuntu")
    assert "KillMode=process" in unit and "User=ubuntu" in unit and "Restart=on-failure" in unit
    assert "Environment=DOCKER_CONFIG=/home/ubuntu/drone-agent/console/docker-client" in unit
    assert "ProtectHome=read-only" in unit
    compose = yaml.safe_load((ROOT / "sim/compose.console.yaml").read_text())
    web = compose["services"]["console"]
    assert web["ports"] == ["127.0.0.1:8768:8768"]
    assert web["read_only"] is True and web["cap_drop"] == ["ALL"]
    assert web["security_opt"] == ["no-new-privileges:true"]
    assert web["networks"] == ["console_ingress"]
    assert compose["networks"] == {"console_ingress": {"driver": "bridge"}}
    assert all("docker.sock" not in value and ".ssh" not in value for value in web["volumes"])
    assert any(value.endswith("/artifacts:/records:ro") for value in web["volumes"])
    assert web["restart"] == "unless-stopped"


def test_serve_comparison_never_hides_changes_to_existing_entries():
    original = {"TCP": {"443": {"HTTPS": True}}, "Web": {"example.test.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5173"}}}}}
    installed = copy.deepcopy(original)
    installed["TCP"]["8447"] = {"HTTPS": True}
    installed["Web"]["example.test.ts.net:8447"] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8768"}}}
    assert MANAGER["other_routes"](original) == MANAGER["other_routes"](installed)
    assert MANAGER["route_matches"](installed, ORIGIN)
    installed["AllowFunnel"] = {"example.test.ts.net:8447": True}
    assert not MANAGER["route_matches"](installed, ORIGIN)
    installed["Web"]["example.test.ts.net:443"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
    assert MANAGER["other_routes"](original) != MANAGER["other_routes"](installed)


def test_verified_pages_survive_restart_and_changed_pages_are_not_served(tmp_path):
    backend = CloudBackend(endpoint=tmp_path / "broker.sock", records=tmp_path / "records", releases=tmp_path / "releases", outputs=tmp_path / "outputs", source_sha="a" * 40)
    directory = backend.outputs / DEPLOYMENT / RUN
    directory.mkdir(parents=True)
    page = directory / "viewer.html"
    page.write_bytes(b"verified page")
    (directory / "page.json").write_text(json.dumps({"run_id": RUN, "deployment_id": DEPLOYMENT, "page_sha256": hashlib.sha256(page.read_bytes()).hexdigest()}))
    bridge = Bridge(None, None)
    backend.restore_pages(bridge)
    assert bridge.pages[RUN] == page and bridge.exports[RUN]["status"] == "ready"
    page.write_bytes(b"modified")
    second = Bridge(None, None)
    backend.restore_pages(second)
    assert second.pages == {}


def test_evidence_path_cannot_escape_mounted_records(tmp_path):
    backend = CloudBackend(endpoint=tmp_path / "broker.sock", records=tmp_path / "records", releases=tmp_path / "releases", outputs=tmp_path / "outputs", source_sha="a" * 40)
    with pytest.raises(ValueError):
        backend.fetch({"deployment_id": "../escape", "run_id": RUN, "seed": 7, "case": "interactive-7"})
    assert not backend.outputs.exists()


def test_actual_asgi_http_server_handles_private_origin_and_refuses_untrusted_requests():
    import uvicorn

    app, calls = application()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(ASGIConsole(app), lifespan="off", access_log=False, proxy_headers=False, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started
        connection = http.client.HTTPConnection(*sock.getsockname(), timeout=5)
        headers = {"Host": "example.test.ts.net:8447", "Origin": ORIGIN, "Content-Type": "application/json"}
        connection.request("GET", "/", headers=headers)
        response = connection.getresponse()
        assert response.status == 200 and app.nonce.encode() in response.read()
        connection.request("POST", "/api/start", json.dumps({"run_id": RUN, "seed": 7}), headers)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert calls == []
        headers["X-Console-Nonce"] = app.nonce
        connection.request("POST", "/api/start", json.dumps({"run_id": RUN, "seed": 7}), headers)
        response = connection.getresponse()
        assert response.status == 202
        response.read()
        assert calls == [{"action": "live_start", "run_id": RUN, "seed": 7}]
        connection.close()
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


def test_cloud_evidence_verifies_files_before_rendering_and_keeps_recordings_unchanged(tmp_path, monkeypatch):
    from drone_agent.console import cloud

    backend = CloudBackend(endpoint=tmp_path / "broker.sock", records=tmp_path / "records", releases=tmp_path / "releases", outputs=tmp_path / "outputs", source_sha="a" * 40)
    case = backend.records / DEPLOYMENT / ("m1-" + RUN) / "interactive-7"
    case.mkdir(parents=True)
    sample = case / "sample.bin"
    sample.write_bytes(b"recorded")
    expected = hashlib.sha256(sample.read_bytes()).hexdigest()
    (case.parent / "suite.json").write_text(json.dumps({"source_sha": "a" * 40, "results": [{"scenario": "interactive", "seed": 7, "artifacts": {"sample.bin": expected}}]}))
    source = backend.releases / DEPLOYMENT / "source"
    source.mkdir(parents=True)
    (source.parent / "manifest.json").write_text(json.dumps({"source_sha": "a" * 40}))
    calls = []

    def render(cases, *, root, output, receipt):
        calls.append((cases, root, receipt))
        output.write_bytes(b"verified evidence view")

    monkeypatch.setattr(cloud, "build_page", render)
    job = {"deployment_id": DEPLOYMENT, "run_id": RUN, "seed": 7, "case": "interactive-7", "source_sha": "a" * 40}
    result = backend.fetch(job)
    assert result["files_checked"] == 1 and calls[0][0] == [case] and calls[0][1] == source
    assert sample.read_bytes() == b"recorded" and set(case.iterdir()) == {sample}
    sample.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        backend.fetch(job)
    assert len(calls) == 1


@pytest.mark.parametrize("defect", [None, "unpublished", "public", "simulation_network"])
def test_container_verification_checks_real_port_publication_and_network_membership(tmp_path, monkeypatch, defect):
    ports = {"8768/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8768"}]}
    mounts = [("/broker", "console/ipc", False), ("/records", "artifacts", False),
              ("/releases", "releases", False), ("/outputs", "console/pages", True)]
    container = {
        "Id": "console", "Image": "sha256:test", "State": {"Running": True},
        "Config": {"User": "1000:1001", "Labels": {"io.drone-agent.component": "tailnet-console", "io.drone-agent.source-sha": "a" * 40}},
        "HostConfig": {"PortBindings": copy.deepcopy(ports), "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"]},
        "NetworkSettings": {"Ports": copy.deepcopy(ports), "Networks": {"drone-agent-cloud_console_ingress": {}}},
        "Mounts": [{"Destination": dest, "Source": str(tmp_path / source), "RW": writable, "Type": "bind"} for dest, source, writable in mounts],
    }
    if defect == "unpublished":
        container["NetworkSettings"]["Ports"] = {"8768/tcp": None}
    elif defect == "public":
        container["NetworkSettings"]["Ports"]["8768/tcp"][0]["HostIp"] = "0.0.0.0"
    elif defect == "simulation_network":
        container["NetworkSettings"]["Networks"]["drone-agent-cloud_simulation"] = {}
    function = MANAGER["inspect_console"]
    monkeypatch.setitem(function.__globals__, "os", SimpleNamespace(getuid=lambda: 1000, getgid=lambda: 1001))
    monkeypatch.setitem(function.__globals__, "command", lambda argv: SimpleNamespace(stdout="console\n" if argv[1] == "ps" else json.dumps([container])))
    if defect:
        with pytest.raises(ValueError):
            function(tmp_path, source_sha="a" * 40)
    else:
        assert function(tmp_path, source_sha="a" * 40)["published_ports"] == ports


def test_cloud_entry_status_does_not_hide_different_runtime_revision(tmp_path, monkeypatch):
    function = MANAGER["status"]
    monkeypatch.setitem(function.__globals__, "read_json", lambda path: {"source_sha": "a" * 40, "origin": ORIGIN})
    monkeypatch.setitem(function.__globals__, "serve_config", lambda: {})
    monkeypatch.setitem(function.__globals__, "route_matches", lambda *_: True)
    monkeypatch.setitem(function.__globals__, "command", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setitem(function.__globals__, "probe", lambda _origin: {"status": "ready", "console_source_sha": "a" * 40, "runtime_source_sha": "b" * 40})
    assert function(tmp_path)["status"] == "unhealthy"
