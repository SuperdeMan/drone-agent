"""Evaluation of the cross-site reference check (D082) on labelled requests: deterministic, no model.

The three operations corpora (`plan_ops_v1`–`v3`, P3 catalog) label what an ideal planner may return for each request.
A request the check rejects must accept a refusal: a hit on a request that must be planned is a false rejection, and
there must be none. Hits on requests that accept a refusal are reported as caught; a cross-project request that accepts
only a refusal and is not hit is reported as missed. The desk cases (`references_desk_v1`, desk catalog) state the
expected result of each request directly.

在带标注的请求上评测跨站点指代检查（D082）：确定性，不调用模型。

三份运营语料（`plan_ops_v1`–`v3`，P3 目录）为每条请求标注了理想规划器可以给出的结果。被检查拒绝的请求必须接受拒答：命中只接受
规划的请求即误拦，必须为 0。命中接受拒答的请求记为拦下；跨项目类只接受拒答、却没有命中的请求记为漏拦。任务台用例
（`references_desk_v1`，任务台目录）直接写明每条请求的期望结果。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml

CORPORA = ("plan_ops_v1", "plan_ops_v2", "plan_ops_v3")
DESK = "eval/adversarial/references_desk_v1.yaml"


def index_for(root: Path, catalog_path: str):
    """The catalog and its reference index, as the service builds them. / 与服务相同方式构建的目录及其名称索引。"""
    from drone_agent.fleet.references import ReferenceIndex
    from drone_agent.fleet.resources import load_catalog
    from drone_agent.mission.registry import Registry

    catalog = load_catalog(root / catalog_path)
    registries = {site_id: Registry(root, scene=root / site.scene) for site_id, site in catalog.sites.items()}
    return catalog, ReferenceIndex.build(catalog, registries, root / catalog_path)


def binding_of(catalog, robot_id: str) -> dict[str, str]:
    site_id = catalog.robots[robot_id].site_id
    return {"project_id": catalog.sites[site_id].project_id, "site_id": site_id, "robot_id": robot_id}


def corpus_report(root: Path, name: str) -> dict:
    from drone_agent.eval.p6_adversarial import CORPORA as FILES

    data = yaml.safe_load((root / FILES[name]).read_text(encoding="utf-8"))
    catalog, index = index_for(root, data["catalog"])
    rows = []
    for case in data["cases"]:
        hits = index.foreign(case["text"], binding_of(catalog, case["robot"]))
        if hits:
            verdict = "caught" if "refused" in case["accept"] else "false_rejection"
        elif case["category"] == "cross_project" and case["accept"] == ["refused"]:
            verdict = "missed"
        else:
            verdict = "clear"
        rows.append({"id": case["id"], "category": case["category"], "robot": case["robot"], "verdict": verdict,
                     "hits": [f"{h['matched']} ({h['kind']} {h['id']})" for h in hits]})
    return {"corpus": name, "cases": len(rows), "verdicts": dict(Counter(r["verdict"] for r in rows)),
            "per_category": {c: dict(Counter(r["verdict"] for r in rows if r["category"] == c))
                             for c in data["categories"]},
            "false_rejections": [r for r in rows if r["verdict"] == "false_rejection"],
            "caught": [r for r in rows if r["verdict"] == "caught"],
            "missed": [r for r in rows if r["verdict"] == "missed"],
            "aliases_sha256": index.aliases_sha256}


def desk_report(root: Path) -> dict:
    data = yaml.safe_load((root / DESK).read_text(encoding="utf-8"))
    catalog, index = index_for(root, data["catalog"])
    rows = []
    for case in data["cases"]:
        hits = index.foreign(case["text"], binding_of(catalog, case["robot"]))
        found = "foreign" if hits else "clear"
        rows.append({"id": case["id"], "robot": case["robot"], "expect": case["expect"], "found": found,
                     "as_expected": found == case["expect"],
                     "hits": [f"{h['matched']} ({h['kind']} {h['id']})" for h in hits]})
    return {"cases": len(rows), "as_expected": sum(r["as_expected"] for r in rows),
            "unexpected": [r for r in rows if not r["as_expected"]], "rows": rows,
            "aliases_sha256": index.aliases_sha256}


def evaluate(root: Path) -> dict:
    corpora = {name: corpus_report(root, name) for name in CORPORA}
    desk = desk_report(root)
    false = sum(len(r["false_rejections"]) for r in corpora.values())
    return {"format": "drone.reference-check-eval/v1", "corpora": corpora, "desk": desk,
            "false_rejections": false, "caught": sum(len(r["caught"]) for r in corpora.values()),
            "missed": sum(len(r["missed"]) for r in corpora.values()),
            "status": "passed" if false == 0 and not desk["unexpected"] else "failed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.root)
    text = json.dumps(result, ensure_ascii=False, indent=1) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in result.items() if k in ("status", "false_rejections", "caught", "missed")}
                     | {"desk": f"{result['desk']['as_expected']}/{result['desk']['cases']}"}))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
