"""Build a read-only, source-bound locator for frozen proposal windows.

为冻结提案窗口构建只读、绑定来源的定位对照页。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import shutil
from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).with_suffix("")
CONTRACT = runpy.run_path(str(Path(__file__).with_name("p6_candidate_groups.py")))
read, digest = CONTRACT["read"], CONTRACT["digest"]
UI_FILES = ("index.html", "viewer.css", "core.js", "app.js")


def encoded(value) -> bytes:
    """Use deterministic JSON bytes without nonfinite numbers. / 使用不含非有限数的确定性 JSON 字节。"""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def inside(root: Path, name: str) -> Path:
    """Reject absolute paths, parent traversal and escaping symlinks. / 拒绝绝对路径、父级跳转与越界符号链接。"""
    relative = Path(name)
    path = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root.resolve()):
        raise ValueError("path leaves package")
    return path


def source_file(root: Path, name: str) -> Path:
    """Allow the documented sibling dataset within its data workspace. / 允许数据工作区内已登记的相邻数据集。"""
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve().parent):
        raise ValueError("source leaves data workspace")
    return path


def image_size(path: Path, expected: str, size: list[int]) -> None:
    """Validate bytes and the coordinate frame before displaying an image. / 展示前校验图像字节与坐标基准。"""
    if digest(path) != expected:
        raise ValueError("image hash mismatch")
    with Image.open(path) as image:
        if list(image.size) != size:
            raise ValueError("image dimensions mismatch")


def load_evidence(batch: Path, attempt: Path, verification: Path, index_file: Path) -> dict:
    """Reject changed records, altered groups and any invented human confirmation.

拒绝被改写的录制、分组和任何预填人工确认。
"""
    repo = batch.resolve().parents[2]
    if not attempt.resolve().is_relative_to(batch.resolve() / "attempts"):
        raise ValueError("attempt outside batch")
    receipt = read(verification)
    if receipt["archive_directory"] != attempt.resolve().relative_to(repo).as_posix():
        raise ValueError("receipt attempt mismatch")
    for name, expected in receipt["artifact_sha256"].items():
        if digest(inside(repo, name)) != expected:
            raise ValueError("archived evidence changed")
    for name, expected in receipt["frozen_inputs_sha256"].items():
        if digest(inside(batch, name)) != expected:
            raise ValueError("frozen input changed")
    contract_path = Path(__file__).with_name("p6_candidate_groups.py")
    if digest(contract_path) != receipt["source_sha256"]["scripts/p6_candidate_groups.py"]:
        raise ValueError("candidate contract changed")
    plan, manifest, config = (read(batch / name) for name in ("plan.json", "input-manifest.json", "config.json"))
    summary, queue, analysis = (read(attempt / name) for name in ("replay-summary.json", "review-queue.json", "analysis.json"))
    results = read(attempt / "cloud/output/results.json")
    reduced = CONTRACT["reduce_results"](manifest, results, config["model"])
    if any(summary[key] != value for key, value in reduced.items()):
        raise ValueError("summary differs from frozen model results")
    if digest(index_file) != analysis["conditional_geometry"]["index_sha256"]:
        raise ValueError("source index changed")
    if (plan["manifest_sha256"] != digest(batch / "input-manifest.json")
            or plan["config_sha256"] != digest(batch / "config.json")
            or summary["results_sha256"] != digest(attempt / "cloud/output/results.json")
            or analysis["review_queue_sha256"] != digest(attempt / "review-queue.json")):
        raise ValueError("source provenance mismatch")
    original = {r["proposal_id"]: r for r in summary["rows"]}
    queued = {r["proposal_id"]: r for r in queue["rows"]}
    if len(queued) != len(queue["rows"]) or set(queued) != set(original):
        raise ValueError("queue proposal coverage mismatch")
    for ident, row in queued.items():
        source = original[ident]
        if (any(row[key] != source[key] for key in ("view_id", "source_bbox", "model_disposition"))
                or row["model_observation"] != source["observation"]):
            raise ValueError("queue model group or geometry changed")
        if any(row[key] is not None for key in ("human_verdict", "reviewer", "reviewed_at", "human_hint_exposure", "instance_bbox")):
            raise ValueError("read-only viewer requires unconfirmed proposals")
        if row["priority"] not in {"first", "remaining"}:
            raise ValueError("invalid review priority")
    if (receipt["formal_release_eligible"] is not False or queue["human_reviews_completed"] != 0
            or queue["formal_release_eligible"] is not False or queue["all_proposals_require_human_confirmation"] is not True):
        raise ValueError("development-only evidence required")
    return {"receipt": receipt, "plan": plan, "manifest": manifest, "config": config,
            "summary": summary, "queue": queue, "analysis": analysis, "index": read(index_file)}


def build(batch: Path, attempt: Path, verification: Path, index_file: Path,
          data_root: Path, input_images: Path, output: Path) -> dict:
    """Copy exact image bytes and bind the read-only UI to existing model evidence.

复制原始图像字节，将只读界面绑定到既有模型证据。
"""
    if output.exists():
        raise ValueError("output must be a new directory")
    evidence = load_evidence(batch, attempt, verification, index_file)
    plan, manifest = evidence["plan"], evidence["manifest"]
    sources = {c["case_id"]: c for c in evidence["index"]["cases"]}
    parents = {p["view_id"]: p for p in plan["parents"]}
    if len(parents) != len(plan["parents"]) or set(parents) != {r["case_id"] for r in manifest}:
        raise ValueError("view parent coverage mismatch")
    targets, copies = [], {}
    for ordinal, row in enumerate(manifest, 1):
        parent = parents[row["case_id"]]
        source = sources[parent["case_id"]]
        size = [source["width"], source["height"]]
        if (source["image_sha256"] != parent["source_image_sha256"] or size != parent["source_size"]
                or source["target_region"]["sha256"] != parent["source_target_sha256"]):
            raise ValueError("source frame binding mismatch")
        original = source_file(data_root, source["image_file"])
        image_size(original, source["image_sha256"], size)
        if len(row["images"]) != 2:
            raise ValueError("two frozen model images required")
        modes = {"original": {"file": "images/" + source["image_sha256"] + ".jpg",
                              "sha256": source["image_sha256"], "size": size}}
        copies[modes["original"]["file"]] = original
        for mode, item in zip(("model", "support"), row["images"], strict=True):
            path = CONTRACT["input_file"](input_images, item)
            image_size(path, item["sha256"], parent["input_size"])
            if size[0] * parent["input_size"][1] != size[1] * parent["input_size"][0]:
                raise ValueError("model image aspect ratio differs from source frame")
            name = "images/" + item["sha256"] + ".jpg"
            modes[mode] = {"file": name, "sha256": item["sha256"], "size": parent["input_size"]}
            copies[name] = path
        targets.append({"view_id": row["case_id"], "case_id": parent["case_id"], "image_id": source["image_id"],
                        "title": source["image_id"] + " · 目标 " + str(ordinal), "size": size, "modes": modes})
    ui_hashes = {name: digest(ASSETS / name) for name in UI_FILES}
    bundle = {"format": "drone.p6-proposal-viewer/v1", "builder_sha256": digest(Path(__file__)),
              "contract_sha256": digest(Path(__file__).with_name("p6_candidate_groups.py")), "ui_sha256": ui_hashes,
              "source_verification_sha256": digest(verification), "source_index_sha256": digest(index_file),
              "source_summary_sha256": digest(attempt / "replay-summary.json"),
              "source_queue_sha256": digest(attempt / "review-queue.json"), "source_plan_sha256": digest(batch / "plan.json"),
              "source_run_id": evidence["receipt"]["run_id"], "targets": targets, "proposals": evidence["queue"]["rows"],
              "source_model_counts": evidence["summary"]["model_disposition_counts"], "readonly": True,
              "new_provider_calls": 0, "human_reviews_created": 0, "independent_ground_truth": False,
              "formal_release_eligible": False, "instance_boxes_established": 0}
    bundle["bundle_id"] = hashlib.sha256(encoded(bundle)).hexdigest()
    output.mkdir(parents=True)
    (output / "images").mkdir()
    for name, path in copies.items():
        shutil.copyfile(path, output / name)
    for name in UI_FILES:
        shutil.copyfile(ASSETS / name, output / name)
    (output / "bundle.json").write_bytes(encoded(bundle))
    (output / "data.js").write_bytes(data_script(bundle))
    return {"bundle_id": bundle["bundle_id"], "bundle_sha256": digest(output / "bundle.json"),
            "data_js_sha256": digest(output / "data.js"), "targets": len(targets), "proposals": len(bundle["proposals"]),
            "priority_proposals": sum(r["priority"] == "first" for r in bundle["proposals"]),
            "copied_images": len(copies), "new_provider_calls": 0, "human_reviews_created": 0,
            "formal_release_eligible": False}


def data_script(bundle: dict) -> bytes:
    """Escape script-sensitive characters while keeping all observations literal.

转义脚本敏感字符，观察说明保持原文。
"""
    text = json.dumps(bundle, ensure_ascii=False, sort_keys=True, allow_nan=False)
    for char, escaped in [("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")]:
        text = text.replace(char, escaped)
    return ("window.PROPOSAL_DATA = " + text + ";\n").encode()


def verify(package: Path, expected_sha: str) -> dict:
    """Check a package against its trusted digest, including UI and every image.

按受信摘要校验包、页面和每张图片。
"""
    if digest(package / "bundle.json") != expected_sha:
        raise ValueError("trusted bundle digest mismatch")
    bundle = read(package / "bundle.json")
    ident = bundle.pop("bundle_id")
    if hashlib.sha256(encoded(bundle)).hexdigest() != ident:
        raise ValueError("bundle identity mismatch")
    bundle["bundle_id"] = ident
    if (package / "data.js").read_bytes() != data_script(bundle):
        raise ValueError("UI data changed")
    for name, expected in bundle["ui_sha256"].items():
        if digest(inside(package, name)) != expected:
            raise ValueError("UI source changed")
    for target in bundle["targets"]:
        for image in target["modes"].values():
            image_size(inside(package, image["file"]), image["sha256"], image["size"])
    return {"bundle_id": ident, "verified_images": len({i["file"] for t in bundle["targets"] for i in t["modes"].values()}),
            "proposals": len(bundle["proposals"]), "readonly": bundle["readonly"], "human_reviews_created": 0,
            "formal_release_eligible": False}


def main() -> None:
    """Expose offline build and byte verification only. / 只提供离线构建和字节校验。"""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("build")
    for name in ("batch", "attempt", "verification", "index", "data-root", "input-images", "output"):
        create.add_argument("--" + name, type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--package", type=Path, required=True)
    check.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.batch, args.attempt, args.verification, args.index, args.data_root, args.input_images, args.output)
    else:
        result = verify(args.package, args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
