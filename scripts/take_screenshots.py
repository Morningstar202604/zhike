"""控制台真实截图生成：python scripts/take_screenshots.py [BASE_URL]

需要 core 运行（默认 http://127.0.0.1:8010，单端口模式）。
管理页需要令牌：设置环境变量 ADMIN_TOKEN 后脚本会自动写入 localStorage；
未设置时只能截到登录门禁页。
输出到 docs/screenshots/*.png。
"""
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010").rstrip("/")
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)


ADMIN = os.environ.get("ADMIN_TOKEN", "")


def inject_token(page):
    """把管理员令牌写进 zustand 持久化条目，避免每页登录。"""
    if not ADMIN:
        return
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    page.evaluate(
        """(tok) => {
            const raw = localStorage.getItem('cs-agent-app');
            const obj = raw ? JSON.parse(raw) : { state: {}, version: 0 };
            obj.state = obj.state || {};
            obj.state.adminToken = tok;
            localStorage.setItem('cs-agent-app', JSON.stringify(obj));
        }""",
        ADMIN,
    )


def shot(page, url, name, full=False, wait=2200, action=None):
    page.goto(f"{BASE}{url}", wait_until="domcontentloaded")
    page.wait_for_timeout(wait)
    if action:
        action(page)
        page.wait_for_timeout(2600)
    page.screenshot(path=str(OUT / name), full_page=full)
    print("saved", name)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    desk = browser.new_page(viewport={"width": 1440, "height": 900})
    inject_token(desk)
    shot(desk, "/dashboard", "dashboard.png", full=True, wait=3200)
    shot(desk, "/sessions", "sessions.png", wait=2000)
    shot(desk, "/operator", "operator.png", wait=2000)
    shot(desk, "/logs", "logs.png", wait=2000)
    shot(desk, "/settings", "settings.png", wait=2000)

    def ask_agent(page):
        page.fill(".composer-bar textarea", "我的订单什么时候发货")
        page.press(".composer-bar textarea", "Enter")

    shot(desk, "/", "visitor-chat.png", wait=2000, action=ask_agent)
    desk.close()

    mob = browser.new_page(viewport={"width": 414, "height": 896}, device_scale_factor=2)
    inject_token(mob)
    shot(mob, "/dashboard", "dashboard-mobile.png", wait=3000)
    shot(mob, "/", "visitor-mobile.png", wait=2000)
    mob.close()

    browser.close()

print("ALL_DONE", time.strftime("%H:%M:%S"))
