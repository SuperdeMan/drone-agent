"""Keep cloud tooling isolated from secrets, unrelated workloads and unsafe archives.

保证云端工具与密钥、无关工作负载及不安全归档隔离。
"""

import copy
import io
import json
import runpy
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLIENT = runpy.run_path(str(ROOT / "scripts/dev_stack.py"))
REMOTE = CLIENT["REMOTE"]


def test_target_is_cloud_without_connection_or_local_docker(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dev_stack.py", "target"])
    CLIENT["main"]()
    result = json.loads(capsys.readouterr().out)
    assert result == {"target": "cloud", "policy": "D023", "local_stack_fallback": False}


@pytest.fixture
def connection_env(monkeypatch, tmp_path):
    for prefix in ("DRONE_AGENT", "CAR_AGENT"):
        for suffix in ("DEPLOY_HOST", "DEPLOY_USER", "SSH_IDENTITY"):
            monkeypatch.delenv(prefix + "_" + suffix, raising=False)
    identity = tmp_path / "test-identity"
    identity.write_text("not a real key")
    monkeypatch.setenv("CAR_AGENT_DEPLOY_HOST", "example.invalid")
    monkeypatch.setenv("CAR_AGENT_SSH_IDENTITY", str(identity))
    return identity


def test_existing_ssh_parameters_can_be_reused_without_env_file(connection_env):
    connection = CLIENT["Connection"].from_environment()
    assert connection.source == "CAR_AGENT" and connection.identity == connection_env
    assert "StrictHostKeyChecking=yes" in connection.arguments()
    assert "BatchMode=yes" in connection.arguments()


@pytest.mark.parametrize("host", ["-oProxyCommand=anything", "server; echo bad", "user@server", "server/path"])
def test_ssh_argument_injection_is_rejected(connection_env, monkeypatch, host):
    monkeypatch.setenv("CAR_AGENT_DEPLOY_HOST", host)
    with pytest.raises(ValueError):
        CLIENT["Connection"].from_environment()


def test_partial_drone_connection_does_not_borrow_another_host_identity(connection_env, monkeypatch):
    monkeypatch.setenv("DRONE_AGENT_DEPLOY_HOST", "different.invalid")
    with pytest.raises(ValueError):
        CLIENT["Connection"].from_environment()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "safe/../../escape", ".env", "nested/.env.local", ".git/config"])
def test_source_archive_rejects_path_escape_and_secret_material(name):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.addfile(tarfile.TarInfo(name))
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(ValueError):
        REMOTE["validate_archive"](archive)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_source_archive_rejects_links_and_special_files(kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        item = tarfile.TarInfo("unsafe")
        item.type, item.linkname = kind, "outside"
        archive.addfile(item)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(ValueError):
        REMOTE["validate_archive"](archive)


def manifest():
    return {"source_sha": "a" * 40, "run_id": "20260919T120000Z-0123abcd", "control_sha256": "b" * 64,
            "files": {name: "c" * 64 for name in ("source.tar", *REMOTE["CONTROL_FILES"])},
            "images": {name: {} for name in REMOTE["IMAGE_REFS"]}}


@pytest.mark.parametrize("defect", ["source", "run_id", "control", "files", "images"])
def test_manifest_refuses_unscoped_deployment_inputs(defect):
    value = manifest()
    if defect == "source":
        value["source_sha"] = "HEAD"
    elif defect == "run_id":
        value["run_id"] = "../../car-agent"
    elif defect == "control":
        value["control_sha256"] = ""
    elif defect == "files":
        value["files"][".env"] = "d" * 64
    else:
        value["images"]["other-project:latest"] = {}
    with pytest.raises(ValueError):
        REMOTE["validate_manifest"](value)


def test_read_only_compose_queries_do_not_create_artifact_directories(tmp_path, monkeypatch):
    deployment = tmp_path / "releases" / "test"
    deployment.mkdir(parents=True)
    (deployment / "manifest.json").write_text(json.dumps(manifest()))
    function = REMOTE["compose"]
    monkeypatch.setitem(function.__globals__, "run", lambda *args, **kwargs: "[]")
    assert function(tmp_path, deployment, ["ps", "--format", "json"]) == "[]"
    assert not (tmp_path / "artifacts").exists()


def test_cloud_compose_has_resource_caps_and_no_host_entrypoints():
    value = yaml.safe_load((ROOT / "sim/compose.cloud.yaml").read_text(encoding="utf-8"))
    assert value["name"] == "drone-agent-cloud"
    assert value["networks"]["simulation"]["internal"] is True
    assert set(value["services"]) == {"sitl", "checks"}
    for service in value["services"].values():
        assert not service.get("ports") and not service.get("devices") and not service.get("privileged")
        assert service["cpus"] <= 1.5 and service["mem_limit"] in {"1g", "2g"}
        assert service["memswap_limit"] == service["mem_limit"]
        assert service["pull_policy"] == "never"
        assert all("/var/run/docker.sock" not in mount for mount in service["volumes"])
    assert value["services"]["checks"]["network_mode"] == "none"


def test_image_identity_binds_content_and_entrypoint_not_engine_id():
    first = {"Id": "engine-a", "Architecture": "amd64", "Os": "linux", "RootFS": {"Layers": ["sha256:layer"]},
             "Config": {"Entrypoint": ["bash", "/opt/drone-sim/entrypoint.sh"], "Env": ["HEADLESS=1"]}}
    second = copy.deepcopy(first)
    second["Id"] = "engine-b"
    assert REMOTE["image_fingerprint"](first) == REMOTE["image_fingerprint"](second)
    second["Config"]["Entrypoint"] = ["different"]
    assert REMOTE["image_fingerprint"](first) != REMOTE["image_fingerprint"](second)


@pytest.mark.parametrize("path", ['file"\n!command', "file\nput another", "file\rquit"])
def test_sftp_batch_cannot_inject_another_command(path):
    with pytest.raises(ValueError):
        CLIENT["sftp_literal"](path)


def test_sftp_paths_support_spaces_without_shell_expansion():
    assert CLIENT["sftp_literal"]("D:/owned path/source.tar") == '"D:/owned path/source.tar"'
