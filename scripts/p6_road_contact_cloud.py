"""Launch one frozen road-contact batch in the existing isolated model network.

在既有隔离模型网络中启动一次已冻结的道路接触探针批次。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from dev_stack import Connection

REPO = Path(__file__).resolve().parents[1]
RUNTIME_IMAGE = "sha256:4f84962ee6b75f899ee50afa153dca439942d9bf518b25d7c780c0b17c2b2377"
RUNNER_SHA = "c3e0d56264b2ed22adb78347494a7f96089ddb81e2ea0261ca0adab0f8682536"


def sha(path: Path) -> str:
    """Hash exact bytes. / 对确切字节求摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: dict) -> None:
    """Never overwrite a prior launch receipt. / 不覆盖先前启动回执。"""
    with path.open("xb") as stream:
        stream.write((json.dumps(value, indent=2) + "\n").encode())


def ssh(connection: Connection, code: str, timeout: int = 60) -> bytes:
    """Do not emit login banners or raw stderr. / 不输出登录横幅及原始标准错误。"""
    result = subprocess.run(["ssh", *connection.arguments(), connection.target, "python3 -"],
                            input=code.encode(), capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"remote process failed with exit {result.returncode}; remote artifacts retained")
    return result.stdout


def launch(package: Path, runner: Path, freeze: str) -> None:
    """Bind the launch to committed metadata and keep every diagnostic artifact.

将启动绑定到已提交元数据，并保留全部诊断产物。
"""
    if (package / "launch.json").exists() or (package / "precall.json").exists():
        raise ValueError("this batch already has launch intent; inspect it instead of retrying")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip() != freeze:
        raise ValueError("HEAD does not match the frozen commit")
    for name in ("scripts/p6_road_contact_cloud.py", "scripts/p6_road_contact.py", "scripts/p6_road_views.py"):
        if (REPO / name).read_bytes() != subprocess.check_output(["git", "show", f"{freeze}:{name}"], cwd=REPO):
            raise ValueError("local tool source differs from frozen commit")
    plan = json.loads((package / "plan.json").read_text(encoding="utf-8"))
    if plan["builder_sha256"] != sha(REPO / "scripts/p6_road_contact.py"):
        raise ValueError("package builder differs from frozen tool")
    if plan["planned_calls"] != 6 or plan["prompt_version"] != "road-region-contact-dev-v3":
        raise ValueError("this launcher is limited to the frozen six-case batch")
    for local, name, expected in [(package / "config.json", "config.json", plan["config_sha256"]),
                                  (package / "inputs/manifest.json", "input-manifest.json", plan["manifest_sha256"]),
                                  (package / "plan.json", "plan.json", sha(package / "plan.json"))]:
        committed = subprocess.check_output(["git", "show", f"{freeze}:eval/s2/p6_candidates_v7/{name}"], cwd=REPO)
        if sha(local) != expected or local.read_bytes() != committed:
            raise ValueError("local package differs from frozen commit")
    if sha(runner) != RUNNER_SHA or plan["expected_runtime"]["runner_sha256"] != RUNNER_SHA:
        raise ValueError("recording runner differs from the existing diagnostic")
    manifest = json.loads((package / "inputs/manifest.json").read_text(encoding="utf-8"))
    if len(manifest) != 6:
        raise ValueError("exactly six requests required")
    for row in manifest:
        for item in row["images"]:
            image = (package / "inputs" / item["file"]).resolve()
            if not image.is_relative_to((package / "inputs").resolve()) or sha(image) != item["sha256"]:
                raise ValueError("image binding mismatch")
    write(package / "precall.json", {"freeze_commit": freeze, "recorded_at_utc": datetime.now(UTC).isoformat(),
          "plan_sha256": sha(package / "plan.json"), "config_sha256": plan["config_sha256"],
          "manifest_sha256": plan["manifest_sha256"], "launcher_sha256": sha(Path(__file__)),
          "planned_calls": 6, "formal_release_eligible": False})
    connection = Connection.from_environment()
    ident = "p6-road-contact-dev-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    remote = "/home/ubuntu/drone-agent/artifacts/" + ident
    packet = package / (ident + ".tar")
    with tarfile.open(packet, "x") as archive:
        archive.add(package / "config.json", arcname="config.json")
        archive.add(runner, arcname="runner.py")
        archive.add(package / "inputs/manifest.json", arcname="input/manifest.json")
        names = {item["file"] for row in manifest for item in row["images"]}
        for name in sorted(names):
            archive.add(package / "inputs" / name, arcname="input/" + name)
    packet_sha = sha(packet)
    write(package / "launch.json", {"run_id": ident, "remote_directory": remote, "packet_sha256": packet_sha,
          "packet_bytes": packet.stat().st_size, "container_runtime_sha": plan["expected_runtime"]["runtime_source_sha"],
          "project_freeze_commit": freeze, "scope": "development probe only; no service deployment or flight"})
    ssh(connection, f"from pathlib import Path\nPath({remote!r}).mkdir(parents=True, exist_ok=False)\n")
    copied = subprocess.run(["scp", *connection.arguments(), str(packet), connection.target + ":" + remote + "/packet.tar"],
                            capture_output=True, timeout=180)
    if copied.returncode:
        raise RuntimeError("packet upload failed; no automatic retry")
    print(json.dumps({"status": "uploaded", "run_id": ident, "bytes": packet.stat().st_size}), flush=True)
    code = f'''import subprocess,json,tarfile,hashlib,os
from pathlib import Path
p=Path({remote!r})
assert hashlib.sha256((p/'packet.tar').read_bytes()).hexdigest()=={packet_sha!r}
with tarfile.open(p/'packet.tar') as t:
 for m in t.getmembers():
  assert m.isfile() and not m.issym()
  target=(p/m.name).resolve();assert target.is_relative_to(p.resolve())
  target.parent.mkdir(parents=True,exist_ok=True)
  target.write_bytes(t.extractfile(m).read())
(p/'output').mkdir()
def identities():
 ids=subprocess.check_output(['docker','ps','--filter','name=drone-agent-cloud','--format','{{{{.Names}}}}|{{{{.ID}}}}']).decode().splitlines()
 return sorted(ids)
before=identities()
network=json.loads(subprocess.check_output(['docker','network','inspect','drone-agent-cloud_desk_model']))[0]
assert network['Internal'] is True
image={RUNTIME_IMAGE!r}
subprocess.run(['docker','image','inspect',image],check=True,stdout=subprocess.DEVNULL)
pre={{'before':before,'network':network['Name'],'network_internal':True,'image_id':image}}
(p/'preflight.json').write_text(json.dumps(pre,indent=2))
args=['docker','run','--rm','--pull','never','--name',{ident!r},'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','1g','--cpus','1','--pids-limit','64','--tmpfs','/tmp:size=64m,mode=1777','--user',f'{{os.getuid()}}:{{os.getgid()}}','--network','drone-agent-cloud_desk_model']
envs={{'DRONE_SOURCE_SHA':{plan['expected_runtime']['runtime_source_sha']!r},'VISION_PROVIDER':'minimax-vl','MINIMAX_API_KEY_FILE':'/model/minimax.key','HTTPS_PROXY':'http://desk-model-proxy:3128','NO_PROXY':'localhost,127.0.0.1','LLM_HTTP_READ_CAP_S':'600','REQUIRE_REAL_PROVIDERS':'on','PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}}
for k,v in envs.items():args+=['-e',k+'='+v]
for src,dst in [(p/'input','/input:ro'),(p/'config.json','/config.json:ro'),(p/'runner.py','/runner.py:ro'),(p/'output','/output'),(Path('/home/ubuntu/drone-agent/secrets/m2-model/minimax.key'),'/model/minimax.key:ro')]:args+=['-v',str(src)+':'+dst]
args += [image,'python3','/runner.py']
with (p/'diagnostic.log').open('wb') as log:
 result=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
after=identities()
receipt={{'exit_code':result.returncode,'resident_container_identities_unchanged':before==after,'after':after,'preflight':pre}}
(p/'execution.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt),flush=True)
'''
    result = ssh(connection, code, timeout=1860)
    (package / "execution.json").write_bytes(result)
    destination = package / "cloud"
    destination.mkdir(exist_ok=False)
    for name in ("output", "preflight.json", "execution.json", "diagnostic.log"):
        copied = subprocess.run(["scp", *connection.arguments(), "-r", connection.target + ":" + remote + "/" + name, str(destination)],
                                capture_output=True, timeout=180)
        if copied.returncode:
            raise RuntimeError("result download failed: " + name)
    execution = json.loads(result)
    if execution["exit_code"] != 0 or execution["resident_container_identities_unchanged"] is not True:
        raise RuntimeError("diagnostic execution or resident isolation check failed; evidence retained")
    print(result.decode(), flush=True)
    print((destination / "output/run.json").read_text(encoding="utf-8"), flush=True)


def main() -> None:
    """Require explicit package, runner and committed revision. / 要求明确指定包、录制器及提交版本。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--freeze", required=True)
    args = parser.parse_args()
    launch(args.package.resolve(), args.runner.resolve(), args.freeze)


if __name__ == "__main__":
    main()
