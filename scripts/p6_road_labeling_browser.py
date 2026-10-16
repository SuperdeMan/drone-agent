# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0"]
# ///
"""Test the workbench only with an explicitly synthetic QA package.

只对明确的合成 QA 包测试工作台，不打开隔离评测照片。
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import threading
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    """Serve only the isolated fixture package. / 只提供隔离夹具包。"""

    def log_message(self, format, *args):
        """Keep request logs quiet. / 关闭请求日志。"""


async def run(package: Path, output: Path) -> None:
    """Exercise real UI actions while keeping every export explicitly QA-only.

通过真实页面操作核对流程，全部导出始终标记为 QA。
"""
    bundle = json.loads((package / "bundle.json").read_bytes())
    if bundle.get("qa_fixture") is not True or len(bundle["samples"]) != 2:
        raise ValueError("browser QA requires exactly two synthetic fixture images")
    if "ai_assistance" in bundle:
        await assisted_run(package, output, bundle)
        return
    output.mkdir(parents=True, exist_ok=False)
    checks, errors, requests = [], [], []
    receipt = {"scope": "synthetic whole-target annotation UI only", "human_labels_created": 0, "model_invocations": 0,
               "started_at": datetime.now(UTC).isoformat(), "bundle_sha256": hashlib.sha256((package / "bundle.json").read_bytes()).hexdigest(),
               "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "formal_release_eligible": False,
               "checks": checks, "errors": errors}

    def check(name, value):
        checks.append({"name": name, "passed": bool(value)})
        if not value:
            raise AssertionError(name)

    async def ready(page):
        await page.locator("#canvasFrame[data-ready='true']").wait_for(state="attached")

    async def draw(page, button, points):
        await page.locator(button).click()
        rect = await page.locator("#canvas").bounding_box()
        for x, y in points:
            await page.mouse.click(rect["x"] + x * rect["width"], rect["y"] + y * rect["height"])
        await page.locator("#finishDraw").click()

    async def export(page, filename):
        async with page.expect_download() as pending:
            await page.locator("#exportBtn").click()
        download = await pending.value
        await download.save_as(output / filename)
        return json.loads((output / filename).read_bytes())

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(package)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            receipt["browser_version"] = browser.version
            context = await browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("request", lambda r: requests.append(r.url))
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            await page.goto(url, wait_until="networkidle")
            await ready(page)
            check("qa_banner_visible", await page.locator("#qaBanner").is_visible())
            check("no_prefilled_answer", await page.locator("#verdict").input_value() == "" and await page.locator("#modelSeen").input_value() == "")
            check("two_sources", await page.locator("#queue button").count() == 2)
            await page.locator("#saveBtn").click()
            check("empty_save_rejected", "0 / 2" in await page.locator("#progress").inner_text() and bool(await page.locator("#message").inner_text()))
            await page.locator("#reviewer").fill("QA_FIXTURE_NOT_HUMAN")
            await page.locator("#timerBtn").click()
            await page.locator("#targetBrief").fill("合成夹具道路，从左侧标记到右侧标记，不是真实资产")
            await draw(page, "#drawTarget", [(.15, .25), (.85, .25), (.85, .75), (.15, .75)])
            await page.locator("#confirmTarget").click()
            check("target_registration_required", "目标已确认" in await page.locator("#targetState").inner_text())
            await page.locator("#verdict").select_option("normal")
            await page.locator("#fullTarget").check()
            await page.locator("#notes").fill("仅用于自动化 QA 的正常结论")
            await page.locator("#modelSeen").select_option("false")
            await page.locator("#sourceSeen").select_option("false")
            await page.locator("#saveBtn").click()
            check("first_record_saved", "1 / 2" in await page.locator("#progress").inner_text())
            first = await export(page, "qa-export-normal.json")
            check("qa_export_not_human", first["qa_fixture"] is True and first["records"][0]["origin"] == "qa_fixture")
            check("phase_is_annotation", first["records"][0]["phase"] == "annotation")
            check("real_clock_ticks", first["records"][0]["active_ms"] > 0)
            check("unknown_identity_preserved", first["records"][0]["asset_id"] == "" and first["records"][0]["grouping_verified"] is False)
            await page.reload(wait_until="networkidle")
            await ready(page)
            check("saved_record_survives_reload", "1 / 2" in await page.locator("#progress").inner_text())
            check("reload_does_not_restart_timer", "继续" in await page.locator("#timerBtn").inner_text())
            await page.locator("#targetBrief").fill("修改后的合成目标，必须重新确认")
            check("target_edit_invalidates_judgment", await page.locator("#verdict").input_value() == "" and await page.locator("#fullTarget").is_disabled())
            await page.locator("#timerBtn").click()
            await page.locator("#confirmTarget").click()
            await page.locator("#verdict").select_option("obstacle")
            await page.locator("#objectKind").select_option("vegetation")
            await draw(page, "#drawObject", [(.4, .4), (.6, .4), (.6, .6), (.4, .6)])
            await page.locator("#fullTarget").check()
            await page.locator("#saveBtn").click()
            updated = await export(page, "qa-export-obstacle.json")
            check("object_type_saved", updated["records"][0]["objects"][0]["defect_type"] == "vegetation")
            check("revision_increases", updated["records"][0]["revision"] == 2)
            await page.locator("#next").click()
            await ready(page)
            check("switch_does_not_copy_label", await page.locator("#verdict").input_value() == "" and await page.locator("#sourceSeen").input_value() == "")
            await page.locator("#timerBtn").click()
            await page.locator("#verdict").select_option("excluded_source")
            await page.locator("#notes").fill("合成的不适用来源，不转换成错目标真值")
            await page.locator("#modelSeen").select_option("false")
            await page.locator("#sourceSeen").select_option("false")
            await page.locator("#saveBtn").click()
            check("excluded_source_can_be_recorded", "2 / 2" in await page.locator("#progress").inner_text())
            final = await export(page, "qa-export-final.json")
            check("excluded_source_not_wrong_target", final["records"][1]["verdict"] == "excluded_source")
            check("no_release_authority", final["formal_release_eligible"] is False and final["reviewer_identity_authenticated"] is False)
            await page.locator("#filter").select_option("pending")
            check("pending_filter_empty", await page.locator("#queue button").count() == 0)
            await page.locator("#filter").select_option("all")
            for width, height in [(1440, 1100), (1024, 900), (768, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                await page.screenshot(path=str(output / f"fixture-{width}.png"), full_page=True)
                check("no_horizontal_overflow_" + str(width), await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
                check("panels_within_viewport_" + str(width), await page.evaluate("[...document.querySelectorAll('header,main,aside')].every(e=>{const r=e.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1})"))
                check("save_button_exists_" + str(width), await page.locator("#saveBtn").is_visible())
            await page.set_viewport_size({"width": 1440, "height": 1100})
            await page.evaluate("document.documentElement.style.zoom='2'")
            await page.screenshot(path=str(output / "fixture-zoom200.png"), full_page=True)
            check("zoom200_no_horizontal_overflow", await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            check("zoom200_panels_within_viewport", await page.evaluate("[...document.querySelectorAll('header,main,aside')].every(e=>{const r=e.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1})"))
            await page.evaluate("document.documentElement.style.zoom='1'")
            await context.close()
            offline = await browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            p2 = await offline.new_page()
            await p2.goto((package / "index.html").resolve().as_uri(), wait_until="load")
            await ready(p2)
            check("file_url_operates", await p2.locator("#queue button").count() == 2)
            await p2.locator("#importFile").set_input_files(output / "qa-export-final.json")
            await p2.locator("#progress").filter(has_text="2 / 2").wait_for()
            check("import_roundtrip", "2 / 2" in await p2.locator("#progress").inner_text())
            await p2.locator("#importFile").set_input_files(output / "qa-export-normal.json")
            await p2.locator("#message").filter(has_text="不是更新").wait_for()
            check("stale_import_rejected", "2 / 2" in await p2.locator("#progress").inner_text())
            await offline.close()
            await browser.close()
            check("no_external_requests", all(r.startswith((f"http://127.0.0.1:{server.server_port}/", "data:")) for r in requests))
            check("no_javascript_errors", not errors)
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        server.shutdown()
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt["passed"] = sum(c["passed"] for c in checks)
        receipt["failed"] = sum(not c["passed"] for c in checks)
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def assisted_run(package: Path, output: Path, bundle: dict) -> None:
    """Exercise assisted adjudication without creating real human evidence. / 验证辅助裁定流程，不创建真实人工证据。"""
    output.mkdir(parents=True, exist_ok=False)
    checks, errors, requests = [], [], []
    receipt = {"scope": "synthetic AI-assisted annotation UI only", "human_labels_created": 0, "model_invocations": 0,
               "started_at": datetime.now(UTC).isoformat(), "bundle_sha256": hashlib.sha256((package / "bundle.json").read_bytes()).hexdigest(),
               "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "formal_release_eligible": False,
               "checks": checks, "errors": errors}

    def check(name, value):
        checks.append({"name": name, "passed": bool(value)})
        if not value:
            raise AssertionError(name)

    async def ready(page):
        await page.locator("#canvasFrame[data-ready='true']").wait_for(state="attached")

    async def export(page, filename):
        async with page.expect_download() as pending:
            await page.locator("#exportBtn").click()
        download = await pending.value
        await download.save_as(output / filename)
        return json.loads((output / filename).read_bytes())

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            receipt["browser_version"] = browser.version
            context = await browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: requests.append(r.url))
            await page.goto((package / "index.html").as_uri(), wait_until="load")
            await ready(page)
            check("qa_banner_visible", await page.locator("#qaBanner").is_visible())
            check("draft_visible_not_saved", await page.locator("#aiPanel").is_visible() and "0 / 2" in await page.locator("#progress").inner_text())
            check("ai_exposure_locked", await page.locator("#modelSeen").is_disabled() and await page.locator("#modelSeen").input_value() == "true")
            check("no_prefilled_human_attestations", await page.locator("#verdict").input_value() == "" and not await page.locator("#aiOriginal").is_checked())
            await page.locator("#useSuggestion").click()
            check("loading_requires_real_clock", await page.locator("#verdict").input_value() == "")
            await page.locator("#reviewer").fill("QA_FIXTURE_NOT_HUMAN")
            await page.locator("#timerBtn").click()
            await page.locator("#useSuggestion").click()
            check("suggestion_loaded", await page.locator("#verdict").input_value() == bundle["samples"][0]["ai_suggestion"]["verdict"])
            check("load_does_not_confirm_target", await page.locator("#fullTarget").is_disabled() and await page.locator("#aiDecision").input_value() == "")
            await page.locator("#confirmTarget").click()
            await page.locator("#fullTarget").check()
            await page.locator("#sourceSeen").select_option("false")
            await page.locator("#aiDecision").select_option("accepted")
            await page.locator("#evaluatedSeen").select_option("false")
            await page.locator("#saveBtn").click()
            check("original_review_required", "0 / 2" in await page.locator("#progress").inner_text())
            await page.locator("#aiOriginal").check()
            await page.locator("#saveBtn").click()
            check("actual_adjudication_saved", "1 / 2" in await page.locator("#progress").inner_text())
            value = await export(page, "qa-accepted.json")
            row = value["records"][0]
            check("ai_origin_retained", row["model_outputs_seen"] is True and row["evaluated_model_outputs_seen"] is False and row["ai_draft_sha256"] == bundle["samples"][0]["ai_suggestion_sha256"])
            check("qa_is_not_human", value["qa_fixture"] is True and row["origin"] == "qa_fixture" and value["formal_release_eligible"] is False)
            check("clock_is_annotation_only", row["active_ms"] > 0 and row["phase"] == "annotation")
            await page.locator("#notes").fill("Synthetic operator correction / 合成操作员修改")
            await page.locator("#saveBtn").click()
            check("editing_requires_resume", "计时" in await page.locator("#message").inner_text())
            await page.locator("#timerBtn").click()
            await page.locator("#saveBtn").click()
            check("changed_draft_not_accepted_unchanged", "修改不符" in await page.locator("#message").inner_text())
            await page.locator("#aiDecision").select_option("revised")
            await page.locator("#saveBtn").click()
            value = await export(page, "qa-revised.json")
            check("revised_decision_exported", value["records"][0]["ai_draft_decision"] == "revised" and value["records"][0]["revision"] == 2)
            await page.reload(wait_until="load")
            await ready(page)
            check("reload_preserves_assistance", await page.locator("#modelSeen").is_disabled() and await page.locator("#aiDecision").input_value() == "revised")
            check("reload_pauses_clock", "继续" in await page.locator("#timerBtn").inner_text())
            await page.locator("#next").click()
            await ready(page)
            check("next_has_no_human_confirmation", not await page.locator("#aiOriginal").is_checked() and await page.locator("#evaluatedSeen").input_value() == "")
            await page.locator("#timerBtn").click()
            await page.locator("#useSuggestion").click()
            check("excluded_source_draft", await page.locator("#verdict").input_value() == "excluded_source")
            await page.locator("#sourceSeen").select_option("false")
            await page.locator("#aiDecision").select_option("accepted")
            await page.locator("#aiOriginal").check()
            await page.locator("#evaluatedSeen").select_option("false")
            await page.locator("#saveBtn").click()
            check("excluded_source_saves_without_fake_target", "2 / 2" in await page.locator("#progress").inner_text())
            final = await export(page, "qa-final.json")
            check("two_real_qa_exports_only", len(final["records"]) == 2 and all(r["origin"] == "qa_fixture" for r in final["records"]))
            await page.locator("#previous").click()
            await ready(page)
            for width, height in [(1440, 1100), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                await page.screenshot(path=str(output / f"assisted-{width}.png"), full_page=True)
                check("no_horizontal_overflow_" + str(width), await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            await page.set_viewport_size({"width": 1440, "height": 1100})
            await page.evaluate("document.documentElement.style.zoom='2'")
            await page.screenshot(path=str(output / "assisted-zoom200.png"), full_page=True)
            check("zoom200_no_horizontal_overflow", await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            await context.close()
            other = await browser.new_context(accept_downloads=True)
            page2 = await other.new_page()
            await page2.goto((package / "index.html").as_uri(), wait_until="load")
            await ready(page2)
            await page2.locator("#importFile").set_input_files(output / "qa-final.json")
            await page2.locator("#progress").filter(has_text="2 / 2").wait_for()
            check("assisted_import_roundtrip", await page2.locator("#aiDecision").input_value() == "revised")
            await other.close()
            await browser.close()
            check("no_external_requests", all(r.startswith((package.as_uri() + "/", "data:")) for r in requests))
            check("no_javascript_errors", not errors)
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt["passed"] = sum(c["passed"] for c in checks)
        receipt["failed"] = sum(not c["passed"] for c in checks)
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """Run an isolated software UI check. / 执行隔离的软件页面检查。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.package.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
