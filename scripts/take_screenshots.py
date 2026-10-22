"""控制台真实截图生成：python scripts/take_screenshots.py [BASE_URL]

需要 core 运行（默认 http://127.0.0.1:8010，单端口模式）。
管理页需要令牌：设置环境变量 ADMIN_TOKEN 后脚本会自动写入 localStorage。
演示数据建议先用 scripts/seed_demo.py 生成独立库并单独起服务：

    python scripts/seed_demo.py
    CS_AGENT_DB=.demo/cs_agent.db CS_AGENT_STATIC_DIR=frontend/dist \
      ADMIN_TOKEN=demo-admin CHANNEL_TOKEN=demo-chan \
      python -m uvicorn core.main:app --port 8012
    ADMIN_TOKEN=demo-admin python scripts/take_screenshots.py http://127.0.0.1:8012

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


def open_run_detail(page):
    page.get_by_text("步骤", exact=True).first.click()
    page.wait_for_timeout(900)


def open_session_drawer(page):
    page.locator("tbody tr").first.click()
    page.wait_for_timeout(900)


def open_action_detail(page):
    page.locator("tbody tr").first.click()
    page.wait_for_timeout(700)


def ask_agent(page):
    page.fill(".composer-bar textarea", "怎么开发票")
    page.press(".composer-bar textarea", "Enter")


def open_operator_dialog(page):
    page.get_by_text("接管并对话").first.click()
    page.wait_for_timeout(1400)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    desk = browser.new_page(viewport={"width": 1440, "height": 900})
    inject_token(desk)
    shot(desk, "/dashboard", "dashboard.png", full=True, wait=3200)
    shot(desk, "/sessions", "sessions.png", wait=2000)
    shot(desk, "/sessions", "session-detail.png", wait=1600, action=open_session_drawer)
    shot(desk, "/actions", "actions.png", wait=2000)
    shot(desk, "/actions", "action-detail.png", wait=1600, action=open_action_detail)
    shot(desk, "/runs", "runs.png", wait=2000, action=open_run_detail)
    shot(desk, "/operator", "operator.png", wait=2200, action=open_operator_dialog)
    shot(desk, "/logs", "logs.png", wait=2000)
    shot(desk, "/settings/params", "settings.png", wait=2000)
    shot(desk, "/settings/kb", "kb.png", wait=2000)
    shot(desk, "/settings/tools", "tools.png", wait=2000)
    shot(desk, "/settings/data", "data.png", wait=2000)
    shot(desk, "/", "visitor-chat.png", wait=2000, action=ask_agent)
    desk.close()

    mob = browser.new_page(viewport={"width": 414, "height": 896}, device_scale_factor=2)
    inject_token(mob)
    shot(mob, "/dashboard", "dashboard-mobile.png", wait=3000)
    shot(mob, "/sessions", "sessions-mobile.png", wait=2200)
    shot(mob, "/", "visitor-mobile.png", wait=2000, action=ask_agent)
    mob.close()

    browser.close()

print("ALL_DONE", time.strftime("%H:%M:%S"))
