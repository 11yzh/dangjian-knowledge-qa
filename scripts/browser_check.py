"""Verify the real browser UI in isolated Chrome and Edge sessions."""

import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright
from app.core import STATE


def check():
    out = STATE / "browser"
    out.mkdir(exist_ok=True)
    people = {
        row.split()[1]: row.split()[2]
        for row in (STATE / "演示账号.txt").read_text("utf-8").splitlines()
    }
    base = "http://127.0.0.1:" + os.environ.get("DANGJIAN_PORT", "8768")
    channels = ["msedge", "chrome"]
    results = {}
    with sync_playwright() as pw:
        for channel in channels:
            browser = pw.chromium.launch(channel=channel, headless=True)
            page = browser.new_page(
                viewport={"width": 1440, "height": 900}, device_scale_factor=1
            )
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.get_by_label("用户名").wait_for()
            if channel == "msedge":
                page.screenshot(path=out / "login.png")

            def enter(name):
                page.get_by_label("用户名").fill(name)
                page.get_by_label("密码", exact=True).fill(people[name])
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_role("heading", name="知识问答", exact=True).wait_for()

            enter("reader")
            if channel == "msedge":
                page.screenshot(path=out / "ask.png")
            page.get_by_role("button", name="知识目录", exact=True).click()
            page.get_by_text("中国共产党章程", exact=False).first.wait_for()
            page.get_by_role("button", name="查看资料").click()
            assert page.get_by_role("link", name="打开原文", exact=True).is_visible()
            page.get_by_role("button", name="关闭", exact=True).click()
            page.get_by_role("button", name="知识问答", exact=True).click()
            if channel == "msedge":
                page.get_by_label("输入问题").fill("预备党员的预备期为多长时间？")
                page.get_by_role("button", name="发送问题").click()
                page.get_by_text("有据回答", exact=True).wait_for(timeout=80000)
                page.screenshot(path=out / "answer.png")
                page.get_by_role(
                    "button", name="中国共产党章程", exact=False
                ).first.click()
                page.get_by_role("heading", name="引用原文").wait_for()
                page.screenshot(path=out / "citation.png")
                page.get_by_role("button", name="关闭", exact=True).click()
            page.get_by_role("button", name="历史会话", exact=True).click()
            page.get_by_role("heading", name="历史会话").wait_for()
            page.get_by_role("button", name="退出", exact=True).click()
            enter("editor")
            for name in ("知识维护", "分类与常见问题", "纠错处理"):
                page.get_by_role("button", name=name, exact=True).click()
                page.get_by_role("heading", name=name, exact=True).wait_for()
                if name == "知识维护" and channel == "msedge":
                    page.screenshot(path=out / "knowledge.png")
            page.get_by_role("button", name="退出", exact=True).click()
            enter("admin")
            for name in ("账号与权限", "安全规则", "模型配置", "运营统计", "审计日志"):
                page.get_by_role("button", name=name, exact=True).click()
                page.get_by_role("heading", name=name, exact=True).wait_for()
                if name == "运营统计" and channel == "msedge":
                    page.screenshot(path=out / "stats.png")
            page.get_by_role("button", name="退出", exact=True).click()
            page.set_viewport_size({"width": 390, "height": 844})
            enter("reader")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            if channel == "msedge":
                page.screenshot(path=out / "mobile.png", full_page=True)
            page.get_by_role("button", name="知识目录", exact=True).focus()
            page.keyboard.press("Enter")
            page.get_by_role("heading", name="知识目录").wait_for()
            results[channel] = {
                "desktop": True,
                "mobile": True,
                "uncaught_errors": errors,
            }
            browser.close()
            assert not errors, errors
    (out / "result.json").write_text(json.dumps(results, indent=2), "utf-8")
    print(results)


if __name__ == "__main__":
    check()
