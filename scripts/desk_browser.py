# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0"]
# ///
"""Browser-level check of the flight desk (D068): the page as a person sees it, through the real entry.

It opens the origin in the installed Edge or Chrome through Playwright (no browser download), waits for the hello
frame and visits every workspace the service offers together with the first object of each list, recording for each
view whether its region rendered, horizontal overflow and console errors, in every requested theme and viewport; it
also renders the fixed M1 page under `/fixed/` without starting anything.
With `--mission` it types a request into the composer, approves the exact package in the page and follows the flight
until the supervisor publishes the independent judge's record. It reaches the service only through the page, so the
page's own frames are what is exercised. The receipt names no tailnet host or operator login; screenshots show the
login and stay in the local output directory. The receipt records what the page displayed; it decides nothing.

Run with `uv run scripts/desk_browser.py --origin <origin> --output <dir> [--mission --text ... --approve]`; uv
installs Playwright for this script only (PEP 723), outside the project's dependencies.

任务台的浏览器级核验（D068）：以人看到的样子、经真实入口检查页面。经 Playwright 用本机已装的 Edge 或 Chrome 打开入口（不下载
浏览器），等待 hello 帧后，逐个访问服务提供的工作区及各列表的第一个对象，在每种主题与视口下记录区域是否渲染、是否横向溢出与
控制台报错；另渲染 `/fixed/` 下的固定 M1 页面，不启动任何任务。加 `--mission` 时在输入框中写下请求、在页面中批准确切的任务包，并跟随飞行直到监管者公布独立裁判记录。它只经页面
访问服务，因此验证的正是页面自己的帧。回执不含 tailnet 主机名与操作者登录名；截图会显示登录名，只留在本机输出目录。回执只记录
页面显示了什么，不做判定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

# Where each workspace shows its list, and how to find the first object of that list in the page's own state.
# 各工作区的列表区域，以及如何从页面自身状态中找到该列表的第一个对象。
LISTS = {"overview": "ovAttention", "missions": "missionList", "workflows": "workflowRuns", "tasks": "taskQueue",
         "fleet": "resourceList", "business": "businessFindings"}
DETAILS = {"missions": "detail", "workflows": "runDetail", "tasks": "taskDetail", "fleet": "fleetDetail",
           "business": "bizDetail"}
FIRST = {"missions": "(missions[0] || {}).mission_id || null",
         "workflows": "workflows && workflows.runs[0] ? workflows.runs[0].run_id : null",
         "tasks": "tasks && tasks.tasks.length ? tasks.tasks[tasks.tasks.length - 1].task_id : null",
         "fleet": "resources && resources.sites[0] && resources.sites[0].docks[0] ? resources.sites[0].docks[0].dock_id : null",
         "business": "business && business.findings[0] ? 'finding/' + business.findings[0].finding_id : null"}
TERMINAL = ("completed", "incomplete", "declined", "delivery_rejected", "rejected", "refused", "planning_failed",
            "cancelled", "dispatch_expired", "withdrawn")
NEVER_FLIES = ("declined", "rejected", "refused", "planning_failed", "cancelled", "dispatch_expired", "withdrawn")
REGION = """(id) => {
  const el = document.getElementById(id);
  const box = el && !el.hidden ? el.getBoundingClientRect() : null;
  const blank = el ? el.querySelector('.blank') : null;
  return {rendered: !!box && box.height > 0 && !(blank && /加载/.test(blank.textContent)),
          overflow_px: document.documentElement.scrollWidth - window.innerWidth,
          workspace: [...document.querySelectorAll('main > .ws')].filter(s => !s.hidden).map(s => s.id)};
}"""
FLIGHT = """() => ({status: current.mission.status, version: current.mission.current_version,
  live: current.live ? [current.live.step_id, current.live.state, current.live.allowed_actions] : null,
  host: host ? host.state : null,
  stage: (document.querySelector('#detail .stepper li[aria-current=step]') || {}).textContent || null,
  judge: current.cloud && current.cloud.judge ? {classification: current.cloud.judge.classification,
    passed: current.cloud.judge.passed, false_success_reports: current.cloud.judge.false_success_reports,
    replay_agrees: current.cloud.judge.replay_agrees} : null,
  report: current.report ? current.report.summary : null,
  images: document.querySelectorAll('#detail .shot img').length,
  shown_judge: (document.querySelector('#detail .verdict b') || {}).textContent || null,
  notice: document.getElementById('notice').hidden ? null : document.getElementById('notice').innerText.replace(/\\s*关闭$/, '')})"""


def redact(value, origin: str, login: str | None):
    """Remove the tailnet host and the operator login from a receipt. / 从回执中去掉 tailnet 主机与操作者登录名。"""
    text = json.dumps(value, ensure_ascii=False)
    text = text.replace(urlsplit(origin).hostname or "", "<tailnet-host>")
    if login:
        text = text.replace(login, "<operator>")
    return json.loads(text)


async def open_desk(browser, origin: str, width: int, height: int, scheme: str, errors: list):
    page = await browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
    page.on("console", lambda m: errors.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    await page.goto(origin + "/#overview", wait_until="domcontentloaded")
    await page.wait_for_function("() => typeof hello !== 'undefined' && hello !== null", timeout=30000)
    await page.wait_for_timeout(2500)
    return page


async def tour(page, shots: Path, label: str) -> dict:
    """Every offered workspace and the first object of its list. / 每个提供的工作区及其列表中的第一个对象。"""
    views = {}
    offered = await page.evaluate("() => WORKSPACES.filter(ws => offered(ws))")
    for ws in offered:
        await page.evaluate(f"location.hash = {json.dumps('#' + ws)}")
        await page.wait_for_timeout(1500)
        views[ws] = await page.evaluate(REGION, LISTS[ws])
        await page.screenshot(path=str(shots / f"{label}-{ws}.png"), full_page=True)
        if ws not in FIRST:
            continue
        first = await page.evaluate(FIRST[ws])
        if not first:
            views[ws + "/first"] = {"rendered": None, "reason": "empty list"}
            continue
        await page.evaluate(f"location.hash = {json.dumps('#' + ws + '/' + first)}")
        await page.wait_for_timeout(2500)
        views[ws + "/first"] = await page.evaluate(REGION, DETAILS[ws])
        await page.screenshot(path=str(shots / f"{label}-{ws}-detail.png"), full_page=True)
    return {"offered": offered, "views": views}


async def fixed(browser, origin: str, shots: Path, scheme: str, errors: list) -> dict:
    """The fixed M1 page under the unified entry: it reads state only; nothing is started. / 统一入口下的固定 M1 页面：只读状态，不启动任何任务。"""
    page = await browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    page.on("console", lambda m: errors.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    response = await page.goto(origin + "/fixed/", wait_until="domcontentloaded")
    await page.wait_for_function("() => document.getElementById('connection').className.includes('good')", timeout=30000)
    await page.wait_for_timeout(2000)
    info = await page.evaluate("""() => ({steps: document.querySelectorAll('#steps .step').length,
      map: !!document.querySelector('#map #ground'), overflow_px: document.documentElement.scrollWidth - window.innerWidth,
      modes: [...document.querySelectorAll('nav.modes a')].map(a => [a.getAttribute('href'), a.getAttribute('aria-current')]),
      result: document.getElementById('resultTitle').textContent, phase: document.getElementById('phase').textContent})""")
    await page.screenshot(path=str(shots / f"fixed-{scheme}.png"), full_page=True)
    await page.close()
    return {"status": response.status if response else None, **info}


async def fly(page, args, shots: Path) -> dict:
    """Submit, approve and follow one mission through the page. / 经页面提交、批准并跟随一个任务。"""
    await page.evaluate("location.hash = '#missions/new'")
    await page.wait_for_timeout(800)
    await page.fill("#text", args.text)
    if args.volume:
        await page.select_option("#volume", args.volume)
    for asset in args.asset or []:
        await page.check(f'#assets input[value="{asset}"]')
    if args.robot and await page.is_visible("#robotRow"):
        await page.select_option("#robot", args.robot)
    known = set(await page.evaluate("() => missions.map(m => m.mission_id)"))
    started = time.monotonic()
    await page.click("#submit")
    await page.wait_for_function("""(known) => /^#missions\\/m-[0-9a-f]{12}$/.test(location.hash)
      && !known.includes(location.hash.slice(10)) && current && current.mission.mission_id === location.hash.slice(10)""",
                                 arg=sorted(known), timeout=args.plan_timeout * 1000)
    record = {"mission_id": await page.evaluate("current.mission.mission_id"),
              "planning_s": round(time.monotonic() - started, 1), "timeline": [], "approved_in_page": False}
    await page.screenshot(path=str(shots / "mission-planned.png"), full_page=True)
    status = await page.evaluate("current.mission.status")
    if status == "awaiting_approval" and args.approve:
        await page.wait_for_selector("#approve:not([disabled])", timeout=30000)
        await page.click("#approve")
        record["approved_in_page"] = True
    deadline, last, flying_shot = time.monotonic() + args.flight_timeout, None, False
    while time.monotonic() < deadline:
        state = await page.evaluate(FLIGHT)
        key = (state["status"], json.dumps(state["live"]), state["host"], state["stage"], state["version"],
               state["notice"])
        if key != last:
            record["timeline"].append({"t": round(time.monotonic() - started, 1), **{k: state[k] for k in (
                "status", "version", "live", "host", "stage", "notice")}})
            last = key
        if state["live"] and not flying_shot:
            await page.screenshot(path=str(shots / "mission-flying.png"), full_page=True)
            flying_shot = True
        if state["status"] in NEVER_FLIES or (state["status"] in TERMINAL and state["judge"]):
            break
        if state["status"] == "awaiting_approval" and (not args.approve or state["notice"]):
            break  # not approving, or the page shows why the approval was refused / 未批准，或页面显示了审批被拒的原因
        await page.wait_for_timeout(2000)
    await page.wait_for_timeout(2500)
    final = await page.evaluate(FLIGHT)
    record.update({"final": final, "timed_out": time.monotonic() >= deadline,
                   "seconds": round(time.monotonic() - started, 1)})
    await page.screenshot(path=str(shots / "mission-final.png"), full_page=True)
    return record


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", required=True, help="the desk origin, e.g. from `dev_stack.py desk-cloud --status`")
    parser.add_argument("--output", type=Path, required=True, help="local directory for the receipt and screenshots")
    parser.add_argument("--channel", default="msedge", choices=("msedge", "chrome"), help="installed browser to drive")
    parser.add_argument("--themes", default="light,dark")
    parser.add_argument("--viewports", default="1440x900,390x844")
    parser.add_argument("--no-fixed", action="store_true", help="skip the fixed M1 page (an entry without /fixed/)")
    parser.add_argument("--mission", action="store_true", help="submit, approve and follow one mission in the page")
    parser.add_argument("--text", default="检查起飞点东侧的红色设备标记，带回一张清晰照片。")
    parser.add_argument("--volume", default="campus_training")
    parser.add_argument("--asset", action="append")
    parser.add_argument("--robot")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--plan-timeout", type=float, default=180)
    parser.add_argument("--flight-timeout", type=float, default=900)
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    shots = args.output / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    receipt = {"format": "drone.desk-browser/v1", "started_at": datetime.now(UTC).isoformat(), "origin": origin,
               "tours": {}, "console_errors": []}
    login = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=args.channel, headless=True)
        receipt["browser"] = {"channel": args.channel, "version": browser.version}
        for theme in [t for t in args.themes.split(",") if t]:
            for viewport in [v for v in args.viewports.split(",") if v]:
                width, height = (int(n) for n in viewport.split("x"))
                errors: list[str] = []
                page = await open_desk(browser, origin, width, height, theme, errors)
                hello = await page.evaluate("() => ({identity: hello.identity, can_write: hello.can_write, "
                                            "source_sha: hello.source_sha, planner: hello.planner, protocol: hello.protocol})")
                login = login or (hello["identity"] or "").split(":", 1)[-1] or None
                receipt["hello"] = {**hello, "identity_scheme": (hello["identity"] or "none").split(":")[0], "identity": None}
                label = f"{theme}-{width}x{height}"
                receipt["tours"][label] = await tour(page, shots, label)
                receipt["console_errors"] += [f"{label}: {e}" for e in errors]
                await page.close()
            if not args.no_fixed:
                errors = []
                receipt.setdefault("fixed", {})[theme] = await fixed(browser, origin, shots, theme, errors)
                receipt["console_errors"] += [f"fixed-{theme}: {e}" for e in errors]
        if args.mission:
            errors = []
            page = await open_desk(browser, origin, 1440, 900, "light", errors)
            receipt["mission"] = await fly(page, args, shots)
            receipt["console_errors"] += [f"mission: {e}" for e in errors]
            await page.close()
        await browser.close()
    receipt["finished_at"] = datetime.now(UTC).isoformat()
    receipt = redact(receipt, origin, login)
    path = args.output / "receipt.json"
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"receipt": str(path), "screenshots": str(shots), "console_errors": len(receipt["console_errors"]),
                      "mission": (receipt.get("mission") or {}).get("final")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
