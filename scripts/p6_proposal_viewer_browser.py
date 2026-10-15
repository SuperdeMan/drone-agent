# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0"]
# ///
"""Check locator alignment and offline behavior in an isolated browser session.

在隔离浏览器会话中检查定位对齐与离线行为。
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
    """Serve the artifact directory only, without request chatter. / 只提供产物目录并关闭请求日志。"""

    def log_message(self, format, *args):
        """Suppress request logs. / 关闭请求日志。"""


async def run(package: Path, output: Path) -> None:
    """Exercise all proposals without creating operator records or model calls.

遍历全部提案，不创建操作员记录或模型调用。
"""
    output.mkdir(parents=True, exist_ok=False)
    checks, errors, requests = [], [], []
    names = ("bundle.json", "data.js", "index.html", "viewer.css", "core.js", "app.js")
    hashes = {n: hashlib.sha256((package / n).read_bytes()).hexdigest() for n in names}
    bundle = json.loads((package / "bundle.json").read_bytes())
    receipt = {"scope": "read-only proposal locator UI", "human_reviews_created": 0, "new_provider_calls": 0,
               "formal_release_eligible": False, "checks": checks, "errors": errors,
               "started_at": datetime.now(UTC).isoformat(), "package_sha256": hashes,
               "browser_qa_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def check(name, value, **details):
        checks.append({"name": name, "passed": bool(value), **details})
        if not value:
            raise AssertionError(name + ": " + str(details))

    async def ready(page):
        await page.locator("#detailFrame[data-ready='true']").wait_for(state="attached")
        await page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")

    async def choose(page, ident):
        await page.locator(f"#queue button[data-proposal='{ident}']").click()
        await ready(page)

    async def box(page):
        return await page.locator("#detailBoxes rect").evaluate("el=>{const r=el.getBBox();return [r.x,r.y,r.width,r.height]}")

    async def vb(page):
        return await page.locator("#detail").get_attribute("viewBox")

    async def selected_visible(page):
        return await page.locator("#queue").evaluate("""q=>{
            const a=q.querySelector('button.active').getBoundingClientRect(),r=q.getBoundingClientRect();
            const sx=r.width/q.offsetWidth,sy=r.height/q.offsetHeight;
            return a.top>=Math.max(r.top,0)-1&&a.bottom<=Math.min(r.top+q.clientHeight*sy,innerHeight)+1&&a.left>=Math.max(r.left,0)-1&&a.right<=Math.min(r.left+q.clientWidth*sx,innerWidth)+1;
        }""")

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(package)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            receipt["browser_version"] = browser.version
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("request", lambda r: requests.append(r.url))
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            await page.goto(url, wait_until="networkidle")
            await ready(page)
            conflict = "view-f9b70d3e59bd-p01"
            check("all_33_proposals_in_queue", await page.locator("#queue button").count() == 33)
            check("starts_at_recorded_spatial_conflict", await page.locator("#proposalId").inner_text() == conflict)
            check("no_review_writer", await page.locator("textarea").count() == 0 and await page.locator("#total").inner_text() == "33")
            for row in bundle["proposals"]:
                await choose(page, row["proposal_id"])
                target = next(t for t in bundle["targets"] if t["view_id"] == row["view_id"])
                w, h = target["size"]
                left, top, right, bottom = row["source_bbox"]
                expected = [left * w, top * h, (right - left) * w, (bottom - top) * h]
                rendered = await box(page)
                check("source_alignment_" + row["proposal_id"], all(abs(a - b) < .002 for a, b in zip(expected, rendered, strict=True)))
                check("paired_selection_" + row["proposal_id"], await page.locator("#overviewBoxes rect.selected").get_attribute("data-proposal") == row["proposal_id"])
            await choose(page, conflict)
            native = await box(page)
            for mode in ("model", "support", "original"):
                await page.locator(f"input[name=mode][value={mode}]").check()
                await ready(page)
                target = next(t for t in bundle["targets"] if t["view_id"] == "view-f9b70d3e59bd")
                check("mode_bytes_and_coordinates_" + mode,
                      await page.locator("#detailPhoto").get_attribute("href") == target["modes"][mode]["file"] and await box(page) == native)
            await page.locator("#reference summary").click()
            check("conflict_is_visible_without_rewriting_model_group", "0 像素" in await page.locator("#geometryNote").inner_text()
                  and "候选" in await page.locator("#modelGroup").inner_text())
            await page.locator("#showAll").uncheck()
            check("one_selected_window", await page.locator("#overviewBoxes rect").count() == 1)
            await page.locator("#showAll").check()
            check("all_target_windows_restored", await page.locator("#overviewBoxes rect").count() == 6)
            context_view = await vb(page)
            await page.locator("#tight").click()
            tight_view = await vb(page)
            check("tight_view_reduces_context", float(tight_view.split()[2]) < float(context_view.split()[2]))
            await page.locator("#zoomIn").click()
            check("zoom_changes_view_not_proposal", float((await vb(page)).split()[2]) < float(tight_view.split()[2]) and await box(page) == native)
            await page.locator("#context").click()
            before = await vb(page)
            area = await page.locator("#detailFrame").bounding_box()
            await page.mouse.move(area["x"] + area["width"] / 2, area["y"] + area["height"] / 2)
            await page.mouse.down()
            await page.mouse.move(area["x"] + area["width"] / 2 + 25, area["y"] + area["height"] / 2 + 20, steps=4)
            await page.mouse.up()
            check("pan_changes_view_not_window", await vb(page) != before and await box(page) == native)
            await page.locator("#priorityOnly").check()
            check("priority_filter_has_12", await page.locator("#queue button").count() == 12)
            await page.locator("#scope").select_option("view-6cda4badce65")
            check("empty_filter_hides_stale_images", await page.locator("#empty").is_visible()
                  and await page.locator("#active").is_hidden() and await page.locator("#overviewPhoto").get_attribute("href") is None)
            await page.locator("#clearFilters").click()
            await ready(page)
            check("clear_filter_keeps_all_33", await page.locator("#queue button").count() == 33)
            await page.locator("#priorityOnly").check()
            await page.locator("#overviewBoxes rect[data-proposal='view-f9b70d3e59bd-p03']").click()
            await ready(page)
            check("overview_selection_clears_incompatible_filter", await page.locator("#proposalId").inner_text() == "view-f9b70d3e59bd-p03"
                  and not await page.locator("#priorityOnly").is_checked() and await page.locator("#queue button").count() == 33)
            await choose(page, conflict)
            await page.locator("#detailFrame").focus()
            await page.keyboard.press("ArrowRight")
            await ready(page)
            check("keyboard_moves_to_next_proposal", await page.locator("#proposalId").inner_text() == "view-f9b70d3e59bd-p02")
            await choose(page, conflict)
            for width, height in [(1440, 1000), (1024, 900), (768, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                await page.evaluate("window.scrollTo(0,0)")
                await ready(page)
                check("no_horizontal_overflow_" + str(width), await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
                bounds = await page.locator("#detailFrame").bounding_box()
                check("usable_image_width_" + str(width), bounds["width"] >= 300, measured_width=bounds["width"])
                check("selected_queue_item_visible_" + str(width), await selected_visible(page))
                await page.screenshot(path=str(output / f"viewport-{width}.png"))
                check("selected_item_visible_after_capture_" + str(width), await selected_visible(page))
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.evaluate("document.body.style.zoom='2'")
            await page.evaluate("window.scrollTo(0,0)")
            await ready(page)
            check("zoom_200_no_horizontal_overflow", await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            area = await page.locator("#detailFrame").bounding_box()
            check("zoom_200_image_remains_usable", area["width"] >= 500, measured_width=area["width"])
            check("zoom_200_coordinates_preserved", await box(page) == native)
            check("zoom_200_selected_queue_item_visible", await selected_visible(page))
            label = await page.locator("#detailBoxes .proposal-label").bounding_box()
            check("zoom_200_id_label_scales_with_page", label["height"] >= 20, measured_height=label["height"])
            await page.screenshot(path=str(output / "zoom-200.png"))
            check("no_local_review_storage", await page.evaluate("localStorage.length===0"))
            local = await context.new_page()
            local.on("pageerror", lambda e: errors.append(str(e)))
            await local.goto((package / "index.html").as_uri() + "#view-f9b70d3e59bd-p03")
            await ready(local)
            check("file_url_and_deep_link", await local.locator("#proposalId").inner_text() == "view-f9b70d3e59bd-p03")
            await local.locator("input[name=mode][value=support]").check()
            await ready(local)
            check("file_url_support_view", "暂定支持范围" in await local.locator("#modeNote").inner_text())
            await browser.close()
            check("no_external_requests", all(r.startswith(url.rsplit('/', 1)[0]) or r.startswith("data:") for r in requests))
            check("no_browser_errors", not errors, error_count=len(errors))
            check("package_bytes_unchanged", hashes == {n: hashlib.sha256((package / n).read_bytes()).hexdigest() for n in names})
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc)[:1000])
        raise
    finally:
        server.shutdown()
        server.server_close()
        receipt["completed_at"] = datetime.now(UTC).isoformat()
        receipt["passed"] = bool(checks) and all(c["passed"] for c in checks) and not errors
        (output / "receipt.json").write_bytes((json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode())
        print(json.dumps({"checks": len(checks), "passed": receipt["passed"], "errors": len(errors)}))


def main() -> None:
    """Use explicit artifact paths and an isolated browser profile. / 使用明确产物路径和隔离浏览器配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.package.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
