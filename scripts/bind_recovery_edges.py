"""Bind validation records to the recovery policy v2 edges from same-revision SITL evidence (D042, WP-M3-14).

An edge is bound only when at least three distinct seeds of the scenario it names passed on the tested revision,
with replay agreement, no problems, no false success and the judge naming that edge as validated. New M3 edges take
their evidence from the M3 suite; edges inherited from `multirotor_m1@v1` take it from the full M1 regression on the
same revision, and only when their definition is byte-for-byte the v1 edge (same content hash). A void case never
counts. `--apply` writes one `validation:` line under each bound edge, keeping the file's comments; the edge content
hash excludes the record, so binding never changes what the edge does.

用同一版本的 SITL 证据为恢复策略 v2 的每条边绑定验证记录（D042，WP-M3-14）。

只有当该边所指场景在被测版本上至少有三个不同种子通过（回放一致、无问题、无虚报成功，且裁判把这条边记为已验证）时
才绑定。M3 新边的证据来自 M3 场景集；继承自 `multirotor_m1@v1` 的边的证据来自同一版本上的 M1 完整回归，并且只有当
其定义与 v1 的边逐字节相同（内容哈希一致）时才承认。作废用例从不计入。`--apply` 在每条已绑定的边下写入一行
`validation:`，保留文件注释；边内容哈希不含该记录，因此绑定从不改变边的行为。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drone_agent.contracts import RecoveryPolicy  # noqa: E402

MIN_SEEDS = 3


def passing_seeds(receipts: list[tuple[str, dict]], sha: str) -> dict[str, dict[int, str]]:
    """validated edge -> seed -> receipt name, from qualifying rows only. / 只取合格行：边 -> 种子 -> 回执名。"""
    found: dict[str, dict[int, str]] = {}
    for name, receipt in receipts:
        if receipt.get("source_sha") != sha or receipt.get("other_containers_before") != receipt.get(
                "other_containers_after"):
            continue
        for row in receipt.get("results", []):
            if (row.get("source_sha") == sha and row.get("passed") is True and row.get("replay_agrees") is True
                    and row.get("problems") == [] and row.get("false_success_reports") == 0
                    and not row.get("void") and row.get("validated_edge")):
                found.setdefault(row["validated_edge"], {})[row["seed"]] = name
    return found


def tested_at(receipts: list[tuple[str, dict]]) -> datetime:
    """The latest deployment time among the receipts (deployment ids start with a UTC stamp). / 回执中最晚的部署时间。"""
    stamps = [datetime.strptime(r["deployment_id"][:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
              for _, r in receipts if re.match(r"^\d{8}T\d{6}Z", r.get("deployment_id", ""))]
    if not stamps:
        raise ValueError("receipts carry no deployment time")
    return max(stamps)


def bind(policy_path: Path, v1_path: Path, sha: str, m3: list[tuple[str, dict]], m1: list[tuple[str, dict]]) -> dict:
    policy, v1 = RecoveryPolicy.from_yaml(policy_path), RecoveryPolicy.from_yaml(v1_path)
    v1_hashes = {e.fault_injection_scenario: e.validation_hash() for e in v1.edges}
    m3_seeds, m1_seeds = passing_seeds(m3, sha), passing_seeds(m1, sha)
    when = tested_at(m3 + m1).isoformat()
    records, unbound = {}, {}
    scenarios = [e.fault_injection_scenario for e in policy.edges]
    if len(set(scenarios)) != len(scenarios) or None in scenarios:
        raise ValueError("every v2 edge needs its own injection scenario")
    for edge in policy.edges:
        scenario = edge.fault_injection_scenario
        inherited = scenario in v1_hashes
        if inherited and v1_hashes[scenario] != edge.validation_hash():
            unbound[scenario] = "inherited edge differs from its v1 definition"
            continue
        seeds = (m1_seeds if inherited else m3_seeds).get(scenario, {})
        if len(seeds) < MIN_SEEDS:
            unbound[scenario] = f"{len(seeds)} passing seeds on {sha[:12]}, {MIN_SEEDS} required"
            continue
        sources = sorted(set(seeds.values()))
        records[scenario] = {
            "scenario_id": scenario, "edge_hash": edge.validation_hash(), "software_revision": sha,
            "tested_at": when,
            "evidence_ref": ("m1_regression" if inherited else "m3_suite") + ":" + ",".join(sources)
            + "#seeds=" + ",".join(str(s) for s in sorted(seeds)),
        }
    return {"policy": f"{policy.policy_id}@{policy.version}", "sha": sha, "bound": records, "unbound": unbound}


def apply(policy_path: Path, records: dict) -> str:
    """Insert or replace one `validation:` line under each bound edge. / 在每条已绑定的边下插入或替换一行 `validation:`。"""
    lines = policy_path.read_text(encoding="utf-8").splitlines()
    output, index = [], 0
    while index < len(lines):
        line = lines[index]
        output.append(line)
        match = re.match(r"^(\s+)fault_injection_scenario:\s*(\S+)\s*$", line)
        index += 1
        if match:
            if index < len(lines) and lines[index].strip().startswith("validation:"):
                index += 1  # an older record is replaced / 旧记录被替换
            record = records.get(match.group(2))
            if record is not None:
                output.append(f"{match.group(1)}validation: {json.dumps(record, sort_keys=True)}")
    return "\n".join(output) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--policy", type=Path, default=ROOT / "configs/recovery_policies/multirotor_m3_v2.yaml")
    parser.add_argument("--v1", type=Path, default=ROOT / "configs/recovery_policies/multirotor_m1_v1.yaml")
    parser.add_argument("--m3", type=Path, nargs="+", required=True, help="remote_m3 suite receipts")
    parser.add_argument("--m1", type=Path, nargs="+", required=True, help="remote_m1 suite receipts")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")

    def load(paths):
        return [(path.name, json.loads(path.read_text(encoding="utf-8-sig"))) for path in paths]

    report = bind(args.policy, args.v1, args.sha, load(args.m3), load(args.m1))
    if args.apply:
        if report["unbound"]:
            raise SystemExit(f"refusing to write a partially bound policy: {report['unbound']}")
        text = apply(args.policy, report["bound"])
        bound = RecoveryPolicy.model_validate(__import__("yaml").safe_load(text))
        if bound.unverified_edges():
            raise SystemExit("the written records do not verify every edge")
        args.policy.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({"bound": sorted(report["bound"]), "unbound": report["unbound"]}, indent=2))
    raise SystemExit(0 if not report["unbound"] else 1)


if __name__ == "__main__":
    main()
