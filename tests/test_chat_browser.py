"""Browser regressions for appending a turn and preserving the reading position."""

import asyncio
from contextlib import contextmanager
import socket
import threading
import time

import pytest
from playwright.sync_api import Error, expect, sync_playwright
import uvicorn


@contextmanager
def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False)
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 8
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(8)


@pytest.mark.parametrize(
    "channel,width,height", [("chrome", 1280, 720), ("msedge", 390, 844)]
)
def test_conversation_scroll_and_inline_reader(s, monkeypatch, channel, width, height):
    from app import model
    from app.main import app

    question = "《支部学习资料归档样例》第一条要求在活动结束后多久提交？"
    sid = s.session()
    for _ in range(7):
        assert s.ask(question, sid).json()["data"]["status"] == "SUCCESS"
    gate = threading.Event()
    generate = model.generate

    async def delayed(cfg, payload, seconds):
        await asyncio.to_thread(gate.wait, 12)
        return await generate(cfg, payload, seconds)

    monkeypatch.setattr(model, "generate", delayed)
    with serve(app) as base, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel=channel)
        except Error as exc:
            if "not found" in str(exc) or "doesn't exist" in str(exc):
                pytest.skip(f"{channel} is not installed")
            raise
        try:
            page = browser.new_page(
                viewport={"width": width, "height": height}, has_touch=width < 500
            )
            errors, downloads = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "download", lambda event: downloads.append(event.suggested_filename)
            )
            page.goto(base)
            page.get_by_label("用户名").fill("U1")
            page.get_by_label("密码", exact=True).fill(s.seed["passwords"]["U1"])
            page.get_by_role("button", name="登录", exact=True).click()
            page.get_by_role("button", name="历史会话", exact=True).click()
            page.get_by_role("button", name="打开", exact=True).click()
            answers = page.locator(".message.assistant")
            expect(answers).to_have_count(7)
            thread = page.locator("#chat-thread")
            assert thread.evaluate(
                "t => t.scrollTop > 100 && t.scrollHeight - t.clientHeight - t.scrollTop < 3"
            )
            page.evaluate(
                "window.originalThread = document.querySelector('#chat-thread')"
            )
            editor = page.get_by_label("输入问题")
            editor.fill(question)
            editor.press("Shift+Enter")
            assert editor.input_value().endswith("\n")
            editor.press("Enter")
            expect(page.locator(".turn-wait")).to_be_visible()
            assert (
                page.locator(".loading-icon").evaluate(
                    "e => getComputedStyle(e).animationName"
                )
                == "waiting-turn"
            )
            expect(page.locator("#send")).to_be_disabled()
            editor.fill("保留草稿")
            thread.evaluate("t => t.scrollTop = 80")
            expect(page.locator("#chat-latest")).to_be_visible()
            gate.set()
            expect(answers).to_have_count(8)
            expect(editor).to_have_value("保留草稿")
            assert thread.evaluate(
                "t => t === window.originalThread && Math.abs(t.scrollTop - 80) < 3"
            )
            page.get_by_role("button", name="回到最新", exact=False).click()
            expect(page.locator("#chat-latest")).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            gate.clear()
            editor.fill(question)
            editor.press("Control+Enter")
            expect(page.locator(".turn-wait")).to_be_visible()
            page.reload()
            expect(page.locator(".turn-wait")).to_be_visible()
            gate.set()
            expect(answers).to_have_count(9)
            expect(page.locator(".turn-wait")).to_have_count(0)
            assert thread.evaluate("t => t.scrollTop > 100")
            page.reload()
            expect(answers).to_have_count(9)
            assert thread.evaluate(
                "t => t.scrollHeight - t.clientHeight - t.scrollTop < 3"
            )
            answers.last.locator(".citations button").first.click()
            with page.expect_popup() as new_page:
                page.get_by_role("link", name="打开此版本原文").click()
            reader = new_page.value
            expect(
                reader.get_by_role("heading", name="支部学习资料归档样例")
            ).to_be_visible()
            assert reader.locator(".reader-sheet img").evaluate(
                "i => i.complete && i.naturalWidth > 500"
            )
            reader.get_by_role("link", name="下一页").click()
            expect(reader.get_by_role("spinbutton", name="页码")).to_have_value("2")
            reader.close()
            page.get_by_role("button", name="关闭", exact=True).click()
            expect(answers).to_have_count(9)
            # A lost read acknowledgement can be retried without appending the same pair twice.
            page.route("**/messages?request_id=*", lambda route: route.abort(), times=1)
            editor.fill(question)
            editor.press("Enter")
            expect(page.locator(".turn-error")).to_be_visible()
            page.locator(".turn-error").get_by_role("button", name="重试").click()
            expect(answers).to_have_count(10)
            assert s.obj("U1", "GET", f"/sessions/{sid}/messages")["total"] == 20

            gate.clear()
            editor.fill(question)
            editor.press("Enter")
            expect(page.locator(".turn-wait")).to_be_visible()
            page.get_by_role("button", name="知识目录", exact=True).click()
            gate.set()
            expect(page.get_by_role("heading", name="知识目录")).to_be_visible()
            page.get_by_role("button", name="知识问答", exact=True).click()
            expect(answers).to_have_count(11)
            assert not errors and not downloads
        finally:
            gate.set()
            browser.close()
