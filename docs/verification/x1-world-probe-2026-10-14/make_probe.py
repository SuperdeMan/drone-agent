"""Build the self-contained launcher of the X1 world probe: the host script with its files embedded, started by nohup.

生成 X1 世界探针的自包含启动脚本：内嵌所需文件的主机侧脚本，由 nohup 启动。
"""
import base64
import json
from pathlib import Path

here = Path(__file__).resolve().parent
root = here.parents[2]
files = {"x1_setup.py": root / "sim/x1_setup.py", "x1_sitl.sh": root / "sim/x1_sitl.sh",
         "world_client.py": here / "world_client.py", "ulog_extract.py": here / "ulog_extract.py"}
encoded = {name: base64.b64encode(path.read_bytes().replace(b"\r\n", b"\n")).decode() for name, path in files.items()}
host = (here / "world_probe_host.py").read_text(encoding="utf-8")
marker = "FILES = {}  #"
assert host.count(marker) == 1
host = host.replace(marker, f"FILES = {json.dumps(encoded)}  #", 1)
payload = f'''import base64, subprocess
from pathlib import Path
work = Path.home() / "drone-agent/scratch/x1worldprobe"
work.mkdir(parents=True, exist_ok=True)
(work / "host.py").write_bytes(base64.b64decode("{base64.b64encode(host.encode()).decode()}"))
subprocess.Popen(["nohup", "python3", str(work / "host.py")], stdout=open(work / "host.log", "wb"),
                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
print("launched", (work / "host.py").stat().st_size)
'''
(here / "launch_payload.py").write_text(payload, encoding="utf-8", newline="\n")
print(len(payload))
