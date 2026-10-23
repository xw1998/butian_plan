"""网页「提交目标」的真实浏览器验收：单人/团队切换、逐成员子任务、重复目标提示。

为什么要有一个真浏览器脚本：
- 这两轮易用性改造的重心在网页表单（模式切换、内容不丢、成员↔子任务配对），
  单元测试只能断言模板里出现过哪些钩子，断不了“点一下真的换了、填的东西还在”；
- 真浏览器还能抓到 JS 运行期报错，这是 HTML 静态断言看不见的。

用法（需要 playwright 与 chromium）：
    python tests/acceptance/web_ui.py [--keep]

退出码：
    0  全部通过
    1  有断言失败（打印每条失败原因）
    2  环境缺 playwright / chromium（这不是产品失败，但也不算通过）

本脚本不写默认数据目录：每次在临时目录起一个真实服务（127.0.0.1 + 本机免令牌）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butian.runtime import http_api  # noqa: E402
from butian.runtime.service import ButianService  # noqa: E402

WEB_ROOT = ROOT / "butian" / "web"


class Checks:
    """收集断言，一次跑完再报告，不因第一条失败就停在半路。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("  PASS " + label)
            return True
        self.failures.append(label + (("：" + detail) if detail else ""))
        print("  FAIL " + label + (("：" + detail) if detail else ""))
        return False

    def equal(self, actual, expected, label: str) -> bool:
        return self.ok(actual == expected, label, f"期望 {expected!r}，实际 {actual!r}")


def start_server(home: Path):
    service = ButianService(home)
    for member_id, name, role in (("host", "派活者", "host"), ("m1", "张三", "worker"), ("m2", "李四", "worker")):
        service.add_member(member_id, name, role=role)
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = WEB_ROOT
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, service, f"http://127.0.0.1:{server.server_address[1]}"


def run(base: str, checks: Checks) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        try:
            page.goto(base, wait_until="networkidle")
            page.click('[data-view="tasks"]')
            page.wait_for_selector('[data-form="goal"]')
            page.wait_for_timeout(500)

            checks.ok(page.is_visible('[data-form="goal"]'), "目标表单渲染")
            checks.ok(page.is_visible('[data-solo-fields]'), "默认是单人模式")
            checks.ok(not page.is_visible('[data-team-fields]'), "默认不显示团队区")

            # 派活者认证（本机模式下是下拉框，没有口令输入框）
            page.select_option("#dispatcherMember", "host")
            page.click("#saveDispatcherAuth")
            page.wait_for_timeout(700)

            page.fill('[data-form="goal"] [name="goal"]', "走一遍易用性脚本")
            page.fill('[data-form="goal"] [name="acceptance"]', "能切换模式")
            page.click('[data-goal-mode="team"]')
            page.wait_for_timeout(200)
            checks.ok(page.is_visible('[data-team-fields]'), "切到团队模式后显示团队区")
            checks.ok(not page.is_visible('[data-solo-fields]'), "切到团队模式后隐藏单人区")
            checks.equal(page.input_value('[data-form="goal"] [name="goal"]'), "走一遍易用性脚本", "切换模式不丢已填目标")

            page.check('[data-member-pick][value="m1"]')
            page.wait_for_timeout(150)
            checks.ok(page.is_enabled('[data-member-task="m1"]'), "勾选成员后其子任务可填")
            checks.ok(not page.is_enabled('[data-member-task="m2"]'), "未勾选成员的子任务不可填")
            page.check('[data-member-pick][value="m2"]')
            page.fill('[data-member-task="m1"]', "整理输入数据")
            page.fill('[data-member-task="m2"]', "复核边界条件")
            page.click('[data-form="goal"] button[type="submit"]')
            page.wait_for_timeout(1500)
            notice = (page.text_content("#notice") or "").strip()
            checks.ok("已派发" in notice or "已创建" in notice, "团队目标提交后给出提示", notice)

            page.click('[data-goal-mode="solo"]')
            page.wait_for_timeout(200)
            checks.ok(page.is_visible('[data-solo-fields]'), "切回单人模式显示单人区")
            checks.equal(page.input_value('[data-form="goal"] [name="goal"]'), "走一遍易用性脚本", "切回单人模式仍保留目标")
            page.click('[data-form="goal"] button[type="submit"]')
            page.wait_for_timeout(1500)
            duplicate_notice = (page.text_content("#notice") or "").strip()
            checks.ok("已创建" in duplicate_notice, "单人目标提交成功", duplicate_notice)

            page.click('[data-form="goal"] button[type="submit"]')
            page.wait_for_timeout(1500)
            repeat_notice = (page.text_content("#notice") or "").strip()
            checks.ok("目标任务" in repeat_notice or "改写" in repeat_notice, "重复目标给出提示但不拦截", repeat_notice)

            checks.ok(not errors, "页面无 JS 报错", "; ".join(errors[:3]))
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="网页提交目标的真实浏览器验收")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录，便于查看落库结果")
    args = parser.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("SKIP playwright 未安装：pip install playwright && playwright install chromium")
        return 2

    folder = Path(tempfile.mkdtemp(prefix="butian-web-e2e-"))
    checks = Checks()
    server = service = None
    try:
        server, service, base = start_server(folder / "runtime")
        print("服务地址 " + base + "，数据目录 " + str(folder))
        try:
            run(base, checks)
        except Exception as exc:  # noqa: BLE001 - 浏览器侧异常一律算失败并打印原因
            checks.failures.append(f"浏览器执行异常：{type(exc).__name__}: {exc}")
            print("  FAIL 浏览器执行异常：" + type(exc).__name__ + ": " + str(exc))
        tasks = [dict(row) for row in service.store.db.execute(
            "SELECT task_id, title, parent_id FROM tasks ORDER BY rowid")]
        parent = [task for task in tasks if not task["parent_id"]]
        children = [task for task in tasks if task["parent_id"]]
        checks.ok(len(parent) >= 2, "团队父任务与单人任务都已落库", f"根任务 {len(parent)} 个")
        checks.equal(len(children), 2, "团队目标为每位成员建立了子任务")
        titles = sorted(task["title"] for task in children)
        checks.equal(titles, ["复核边界条件", "整理输入数据"], "子任务标题与成员输入一一对应")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if service is not None:
            service.close()

    print(f"\n{checks.passed} 项通过，{len(checks.failures)} 项失败")
    if checks.failures:
        for failure in checks.failures:
            print(" - " + failure)
        print("数据目录保留在 " + str(folder))
        return 1
    if not args.keep:
        print("数据目录 " + str(folder) + "（可用 --keep 保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
