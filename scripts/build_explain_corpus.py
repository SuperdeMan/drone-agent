"""Build the frozen explanation corpus explain_v1 (WP-P6-05, D083) once; the views are then committed with digests.

Real service views of mission states, trimmed to the fields the fact table reads:
  planning path, on the desk catalog: a cross-site reference, a planner refusal, an unregistered volume, an energy
  block, an asset outside the selected scope, a version awaiting approval and one the approver declined;
  the closed loop: an unverified inspection that the policy re-planned, and the same mission completed afterwards;
  the P1 S0 world: a dock lid jam before the claim, maintenance between approval and claim, and an approval that lapsed
  before the claim.
Each state gets questions in Chinese and English, and two states a change request; the decisive facts are named per
state. Usage: uv run python scripts/build_explain_corpus.py

一次性生成冻结的解释语料 explain_v1（WP-P6-05，D083）；之后连同摘要提交视图。内容是服务对各任务状态给出的真实视图，裁剪到事实表
会读取的字段：任务台目录上的规划路径（跨站点指代、规划拒答、未登记体积、能源拦截、越出所选范围、待审批、审批人驳回）；闭环中核验
未通过后由策略重规划的巡检，以及之后完成的同一任务；P1 S0 世界中领取前机场舱盖卡住、审批与领取之间进入维护、领取前审批过期。每个
状态配中英文问题，另有两个状态配修改请求；决定性事实按状态写明。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

OUT = ROOT / "eval/adversarial/explain_v1"
OPERATOR = "harness:p5-operator"


def trim(view: dict) -> dict:
    """Only what the fact table reads; no login, no package, no journal. / 只保留事实表读取的字段；不含登录名、任务包与账本。"""
    versions = []
    for v in view.get("versions") or []:
        events = [{"kind": e["kind"], "data": e["data"]} for e in v.get("events") or []
                  if e.get("kind") in ("step_outcome", "mission_result")]
        approval = v.get("approval")
        versions.append({
            "version": v["version"], "status": v["status"], "origin": v["origin"],
            "spec": {"targets": [{"asset_id": t.get("asset_id")} for t in (v.get("spec") or {}).get("targets", [])]},
            "diff": v.get("diff"),
            "planner": {k: (v.get("planner") or {}).get(k) for k in ("status", "decline_reason", "prompt_version", "notes")}
            if v.get("planner") else None,
            "admission": {k: (v.get("admission") or {}).get(k) for k in ("accepted", "codes", "energy_upper_fraction")}
            if v.get("admission") else None,
            "approval": {k: approval.get(k) for k in ("approved_at", "expires_at")} if approval else None,
            "decision": {k: (v.get("decision") or {}).get(k) for k in ("blocked_at", "codes", "reason")},
            "events": events})
    dispatch = view.get("dispatch") or {}
    return {
        "mission": {k: view["mission"].get(k) for k in ("mission_id", "status", "current_version", "robot_id", "replans")},
        "request": {k: (view.get("request") or {}).get(k) for k in ("text", "approved_volume_id", "asset_ids")},
        "binding": {k: (view.get("binding") or {}).get(k) for k in ("project_id", "site_id", "robot_id", "legacy")}
        if view.get("binding") else None,
        "versions": versions,
        "evidence": [{"evidence_id": e["evidence_id"], "version": e["version"], "step_id": e["step_id"],
                      "verification": {k: (e.get("verification") or {}).get(k) for k in ("final_verdict", "checks")}}
                     for e in view.get("evidence") or []],
        "issues": [{k: i.get(k) for k in ("code", "layer", "message", "affected")} for i in view.get("issues") or []],
        "dispatch": {"decisions": [{k: d.get(k) for k in ("version", "stage", "verdict", "reasons")}
                                   for d in dispatch.get("decisions") or []],
                     "cancel": dispatch.get("cancel")} if dispatch else None,
        "operations": [{k: o.get(k) for k in ("action", "version", "accepted", "reason")}
                       for o in view.get("operations") or []],
        "report": {k: (view.get("report") or {}).get(k) for k in ("summary", "targets", "all_targets_completed")}
        if view.get("report") else None,
    }


def plan(*assets: str) -> dict:
    from drone_agent.planner.draft import TOOL_NAME

    return {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": {
        "decision": "plan", "decline_reason": "", "goal": "Inspect " + ", ".join(assets), "goal_type": "inspect",
        "approved_volume_id": "campus_training",
        "tasks": [{"task_id": f"inspect_{a}", "skill_id": "skill.inspect.asset", "asset_id": a} for a in assets],
        "notes": ""}}]}


def decline(reason: str) -> dict:
    from drone_agent.planner.draft import TOOL_NAME

    return {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": {
        "decision": "decline", "decline_reason": reason, "goal": "", "goal_type": "inspect",
        "approved_volume_id": "campus_training", "tasks": [], "notes": ""}}]}


async def planning_states(tmp: Path) -> dict[str, dict]:
    from drone_agent.fleet.api import dispatch
    from drone_agent.providers import KeyedScriptedProvider
    from tests.fleet.test_references import service

    texts = {"refused": "Inspect the marker over there.", "energy": "Inspect every marker at this site.",
             "scope": "Inspect the red marker.", "awaiting": "Inspect the red marker of site B.",
             "declined": "Inspect the red marker at site A."}
    provider = KeyedScriptedProvider({
        texts["refused"]: decline("'over there' does not pick a marker: asset_red_b, asset_blue_b or road_north_b?"),
        texts["energy"]: plan("asset_red_b", "asset_blue_b", "road_north_b"),
        texts["scope"]: plan("asset_blue_b"), texts["awaiting"]: plan("asset_red_b"),
        texts["declined"]: plan("asset_red")})
    svc = service(tmp, provider)
    states = {}

    async def submit(name, robot, text, volume="campus_training", assets=()):
        reply = await dispatch(svc, {"method": "missions.submit", "actor": OPERATOR, "trust": "first_party",
                                     "params": {"project_id": "fleet_s0", "robot_id": robot, "text": text,
                                                "volume_id": volume, "asset_ids": list(assets),
                                                "idempotency_key": f"explain-{name}"}})
        assert reply["ok"], reply
        return reply["result"]["mission"]["mission_id"]

    ids = {"foreign": await submit("foreign", "uav_fb", "Inspect the red equipment marker at site A."),
           "refused": await submit("refused", "uav_fb", texts["refused"]),
           "volume": await submit("volume", "uav_fb", "Inspect the red marker.", volume="harbor_zone"),
           "energy": await submit("energy", "uav_fb", texts["energy"]),
           "scope": await submit("scope", "uav_fb", texts["scope"], assets=["asset_red_b"]),
           "awaiting": await submit("awaiting", "uav_fb", texts["awaiting"]),
           "declined": await submit("declined", "uav_fa", texts["declined"])}
    reply = await dispatch(svc, {"method": "decline", "actor": OPERATOR, "trust": "first_party",
                                 "params": {"mission_id": ids["declined"], "version": 1,
                                            "reason": "not today, the yard is busy"}})
    assert reply["ok"], reply
    for name, mission_id in ids.items():
        states[name] = trim(svc._view(mission_id))
    return states


async def loop_states(tmp: Path) -> dict[str, dict]:
    from tests.fleet.harness import build_loop, request

    loop = build_loop(tmp)
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    loop.service.approve(mission_id, 1, approver="tailnet:operator@example.test",
                         package_hash=view["versions"][0]["package_hash"])
    await loop.sync()
    await loop.fly(loop.inbox_package(), epoch=1, frames=[None, None, None])
    await loop.sync()
    states = {"replanned": trim(loop.service._view(mission_id))}
    await loop.fly(loop.inbox_package(), epoch=2)
    await loop.sync()
    states["completed"] = trim(loop.service._view(mission_id))
    return states


def p1_states(tmp: Path) -> dict[str, dict]:
    subprocess.run([sys.executable, "-m", "drone_agent.eval.p1_world", "--output", str(tmp / "p1"), "--scenario",
                    "p1_f06_lid_jam,p1_f11_before_claim", "--seeds", "7"], cwd=ROOT, check=True,
                   env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")})
    jam = json.loads((tmp / "p1/p1_f06_lid_jam-7/service-export/views.json").read_text(encoding="utf-8"))
    claim = json.loads((tmp / "p1/p1_f11_before_claim-7/service-export/views.json").read_text(encoding="utf-8"))
    ordered = sorted(claim.values(), key=lambda v: v["mission"]["created_at"])
    return {"lid_jam": trim(next(iter(jam.values()))), "maintenance": trim(ordered[0]), "lapsed": trim(ordered[1])}


QUESTIONS = {
    "foreign": ("为什么这个请求没有进入审批？", "Why was this request not sent for approval?"),
    "refused": ("规划器为什么拒答？我该怎么说？", "Why did the planner decline, and what should I say instead?"),
    "volume": ("为什么被拒绝？", "Why was this request rejected?"),
    "energy": ("为什么准入没有通过？", "Why did admission reject this plan?"),
    "scope": ("为什么被拦下了？", "What blocked this mission?"),
    "awaiting": ("现在卡在哪一步，需要谁处理？", "What is this mission waiting for?"),
    "declined": ("这个任务为什么结束了？", "Why did this mission end?"),
    "replanned": ("第一版为什么没完成？现在是第几版？", "Why did the first version not complete, and what is running now?"),
    "completed": ("任务完成了吗？证据核验了吗？", "Did the inspection complete with verified evidence?"),
    "lid_jam": ("为什么一直没起飞？", "Why did this mission never take off?"),
    "maintenance": ("这个任务为什么被取消了？", "Why was this mission cancelled?"),
    "lapsed": ("为什么还没起飞？之前的审批还能用吗？", "Why has it not flown, and does the earlier approval still hold?"),
}
CHANGES = {"foreign": "改成检查本站 B 的红色标记吧，帮我写一下新的请求。",
           "awaiting": "Change it to inspect only the blue marker instead and write the new request."}


def decisive(facts: list[dict], state: str) -> list[str]:
    """The facts an answer should rest on, by state. / 按状态给出回答应依据的事实。"""
    by_kind: dict[str, list[str]] = {}
    for fact in facts:
        by_kind.setdefault(fact["kind"], []).append(fact["id"])
    if state in ("foreign", "volume", "energy", "scope"):
        return by_kind.get("issue", []) + [f["id"] for f in facts if f["kind"] == "admission" and f.get("codes")]
    if state == "refused":
        return by_kind.get("planner", [])
    if state in ("awaiting", "lapsed"):
        return [f for f in by_kind.get("missing", []) if f.startswith("missing:approval")] + by_kind.get("approval", []) + \
            by_kind.get("dispatch", [])
    if state == "declined":
        return by_kind.get("decision", []) + ["mission"]
    if state == "replanned":
        return [f["id"] for f in facts if f["kind"] in ("step", "evidence") and f.get("effect_verdict", f.get("verdict"))
                != "verified"] + [f for f in by_kind.get("version", []) if f != "version:1"]
    if state == "completed":
        return ["report"] + by_kind.get("evidence", [])
    return by_kind.get("dispatch", []) + by_kind.get("cancel", []) + by_kind.get("operation", [])


async def main() -> None:
    from drone_agent.fleet.explain import facts_of

    with tempfile.TemporaryDirectory(prefix="explain-corpus-", ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        states = {**await planning_states(tmp / "plan"), **await loop_states(tmp / "loop"), **p1_states(tmp)}
    (OUT / "views").mkdir(parents=True, exist_ok=True)
    cases, views = [], {}
    for name, view in states.items():
        text = json.dumps(view, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
        path = OUT / "views" / f"{name}.json"
        path.write_text(text, encoding="utf-8", newline="\n")
        views[name] = {"file": f"views/{name}.json", "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                       "status": view["mission"]["status"]}
        facts = facts_of(view)
        needed = decisive(facts, name)
        assert needed, f"{name}: no decisive fact in {[f['id'] for f in facts]}"
        for language, question in zip(("zh", "en"), QUESTIONS[name]):
            cases.append({"id": f"{name}-{language}", "state": name, "question": question, "decisive": needed,
                          "change": False})
        if name in CHANGES:
            cases.append({"id": f"{name}-change", "state": name, "question": CHANGES[name], "decisive": needed,
                          "change": True})
    manifest = {"format": "drone.explain-corpus/v1", "corpus": "explain_v1", "views": views, "cases": cases}
    (OUT / "manifest.yaml").write_text(
        "# Frozen explanation corpus (WP-P6-05, D083), built by scripts/build_explain_corpus.py; never edited by hand.\n"
        "# 冻结的解释语料（WP-P6-05，D083），由 scripts/build_explain_corpus.py 生成；不手工修改。\n"
        + yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8", newline="\n")
    print(json.dumps({name: v["status"] for name, v in views.items()}, ensure_ascii=False), len(cases), "cases")


if __name__ == "__main__":
    asyncio.run(main())
