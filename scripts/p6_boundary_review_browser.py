# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0"]
# ///
"""Exercise the offline workbench with isolated QA fixtures. / 用隔离的 QA 夹具检查离线复核页。"""
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
    """Serve only the selected package without request logs. / 仅提供选定包，关闭请求日志。"""

    def log_message(self, format, *args):
        """Suppress local request chatter. / 关闭本地请求日志。"""


async def run(package: Path, output: Path) -> None:
    """Keep screenshots, exports and an exact source receipt outside Git. / 将截图、导出与准确源码回执留在 Git 外。"""
    output.mkdir(parents=True, exist_ok=False)
    checks, errors = [], []
    receipt = {"scope": "offline boundary review UI only", "test_data": "QA_FIXTURE_NOT_HUMAN",
               "human_reviews_created": 0, "formal_release_eligible": False, "checks": checks, "errors": errors,
               "started_at": datetime.now(UTC).isoformat(),
               "package_sha256": {name: hashlib.sha256((package / name).read_bytes()).hexdigest()
                                  for name in ("bundle.json", "data.js", "index.html", "review.css", "core.js", "app.js")},
               "browser_qa_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def check(name, passed, **details):
        checks.append({"name": name, "passed": bool(passed), **details})
        if not passed:
            raise AssertionError(name + ": " + str(details))

    async def ready(page):
        await page.locator("#photo").evaluate("el => { const im = new Image(); im.src=el.getAttribute('href'); return im.decode(); }")

    async def export(page, name):
        async with page.expect_download() as info:
            await page.locator("#exportButton").click()
        file = output / name
        await (await info.value).save_as(file)
        return json.loads(file.read_text(encoding="utf-8"))

    async def import_value(page, value):
        await page.locator("#importFile").set_input_files({"name": "fixture.json", "mimeType": "application/json",
                                                          "buffer": json.dumps(value).encode()})
        await page.wait_for_function("document.getElementById('message').textContent.startsWith('导入')")

    async def begin(page):
        await page.locator("#reviewer").fill("QA_FIXTURE_NOT_HUMAN")
        await page.locator("#start").click()
        await page.locator("#notes").fill("QA_FIXTURE_NOT_HUMAN — synthetic browser exercise, not a business label.")

    async def adopt(page):
        await page.locator("#hintPanel summary").click()
        await page.locator("#adopt").click()

    async def select(page, item):
        await page.locator(f"button[data-item='{item}']").click()
        await ready(page)

    async def draw(page, layer, points):
        await page.locator("#drawLayer").select_option(layer)
        await page.locator("#draw").click()
        for x, y in points:
            pos = await page.locator("#canvas").evaluate("""(el,xy)=>{const p=el.createSVGPoint();
                p.x=xy[0]*3999;p.y=xy[1]*2999;const q=p.matrixTransform(el.getScreenCTM());return {x:q.x,y:q.y};}""", [x, y])
            await page.mouse.click(pos["x"], pos["y"])
        await page.locator("#finish").click()

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(package)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            receipt["browser_version"] = browser.version
            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("requestfailed", lambda r: errors.append(str(r.failure)))
            page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            await page.goto(url, wait_until="networkidle")
            await ready(page)
            check("empty_initial_records", await page.locator("#progress").inner_text() == "0 / 19")
            check("hints_collapsed", await page.locator("#hintPanel").get_attribute("open") is None)
            blank = await export(page, "empty-qa-export.json")
            check("empty_export_no_fabricated_labels", blank["roads"] == [] and blank["entities"] == [])
            await page.locator("#save").click()
            check("empty_form_cannot_register", await page.locator("#progress").inner_text() == "0 / 19")
            for width, height in [(1440, 1000), (1024, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                overflow = await page.evaluate("document.documentElement.scrollWidth-innerWidth")
                check(f"viewport_{width}", overflow <= 1, overflow_px=overflow)
                await page.screenshot(path=str(output / f"viewport-{width}.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.evaluate("document.body.style.zoom='2'")
            check("zoom_200_percent", await page.evaluate("document.documentElement.scrollWidth-innerWidth") <= 1)
            image_fraction = await page.locator("#canvas").evaluate("el=>el.getBoundingClientRect().width/innerWidth")
            check("zoom_200_retains_usable_image_width", image_fraction >= .45, viewport_fraction=image_fraction)
            await page.screenshot(path=str(output / "zoom-200.png"), full_page=True)
            await page.evaluate("document.body.style.zoom='1'")
            ids = await page.locator("#queue button").evaluate_all("bs=>bs.map(b=>b.dataset.item)")
            for ident in ids:
                await select(page, ident)
                check("source_loaded_" + ident, await page.locator("#photo").get_attribute("href") is not None)
            await select(page, ids[0])
            await begin(page)
            await adopt(page)
            await page.locator("#toggleAI").click()
            await page.locator("#toggleSupport").click()
            await page.locator("#save").click()
            check("road_registered", await page.locator("#progress").inner_text() == "1 / 19")
            await select(page, "E01")
            await begin(page)
            await adopt(page)
            await page.locator("#toggleBoxes").click()
            await page.locator("#entityKind").select_option("physical")
            await page.locator("#targetRelation").select_option("inside")
            await page.locator("#groupConfirmed").check()
            await page.locator("#locate").click()
            await page.screenshot(path=str(output / "entity-qa.png"), full_page=True)
            await page.locator("#save").click()
            check("entity_registered", await page.locator("#progress").inner_text() == "2 / 19")
            initial = await export(page, "registered-qa-export.json")
            check("fixture_and_exposure_preserved", all(r["origin"] == "qa_fixture" and r["ai_hints_viewed"] for r in initial["roads"] + initial["entities"]))
            await select(page, ids[0])
            await page.locator("#save").click()
            check("road_revision_marks_entity_stale", (await page.locator("button[data-item='E01']").inner_text()).endswith("道路已变更"))
            await page.locator("#exportButton").click()
            check("stale_export_blocked", "道路" in await page.locator("#message").inner_text())
            await select(page, "E01")
            await page.locator("#save").click()
            final = await export(page, "rechecked-qa-export.json")
            check("explicit_recheck_uses_new_revision", final["entities"][0]["road_revision"] == final["roads"][0]["revision"] != initial["roads"][0]["revision"])
            await page.reload(wait_until="networkidle")
            check("reload_restores_records_but_pauses_timer", await page.locator("#progress").inner_text() == "2 / 19" and "继续" in await page.locator("#start").inner_text())
            await select(page, ids[2])
            await begin(page)
            await draw(page, "core", [[.2, .2], [.7, .2], [.7, .7], [.2, .7]])
            await draw(page, "outer", [[.1, .1], [.8, .1], [.8, .8], [.1, .8]])
            await page.locator("#save").click()
            check("manual_polygons_can_be_registered", await page.locator("#progress").inner_text() == "3 / 19")
            drawn = await export(page, "drawn-qa-export.json")
            check("manual_drawing_no_silent_ai_copy", drawn["roads"][1]["decision"] == "revised" and not drawn["roads"][1]["ai_hints_viewed"])
            fresh_context = await browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            restored = await fresh_context.new_page()
            restored.on("pageerror", lambda e: errors.append(str(e)))
            await restored.goto(url)
            await import_value(restored, final)
            check("import_restores_form_and_records", await restored.locator("#progress").inner_text() == "2 / 19" and await restored.locator("#notes").input_value() == final["roads"][0]["notes"])
            check("import_export_roundtrip", await export(restored, "roundtrip-qa-export.json") == final)
            for name, mutate in [
                ("wrong_bundle", lambda v: v.update(bundle_id="foreign")),
                ("authority", lambda v: v.update(formal_release_eligible=True)),
                ("wrong_image", lambda v: v["entities"][0].update(image_sha256="foreign")),
                ("conflicting_review", lambda v: v["roads"][0].update(notes="Conflicting synthetic record")),
            ]:
                value = json.loads(json.dumps(final))
                mutate(value)
                await restored.locator("#message").evaluate("el=>el.textContent=''")
                await import_value(restored, value)
                check("reject_" + name, (await restored.locator("#message").inner_text()).startswith("导入失败"))
                check("preserve_after_" + name, await restored.locator("#progress").inner_text() == "2 / 19")
            file_page = await fresh_context.new_page()
            file_page.on("pageerror", lambda e: errors.append(str(e)))
            await file_page.goto((package / "index.html").as_uri())
            await ready(file_page)
            check("file_url_loads_without_server", await file_page.locator("#queue button").count() == 19)
            await import_value(file_page, final)
            check("file_url_import_export", await export(file_page, "file-qa-export.json") == final)
            await browser.close()
            check("no_browser_errors", not errors, error_count=len(errors))
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc)[:1000])
        raise
    finally:
        server.shutdown()
        server.server_close()
        receipt["completed_at"] = datetime.now(UTC).isoformat()
        receipt["passed"] = all(c["passed"] for c in checks) and not errors
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"checks": len(checks), "passed": receipt["passed"], "errors": len(errors)}))


def main() -> None:
    """Accept explicit local paths, never the operator's browser profile. / 使用明确本地路径，不读取操作员浏览器配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.package.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
