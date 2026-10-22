"""录制产品演示视频（真实浏览器，带字幕条）。

前置：
    python scripts/seed_demo.py
    CS_AGENT_DB=.demo/cs_agent.db CS_AGENT_STATIC_DIR=frontend/dist \
      ADMIN_TOKEN=demo-admin CHANNEL_TOKEN=demo-chan \
      USER_LLM_API_KEY=demo USER_LLM_BASE_URL=http://127.0.0.1:9099/v1 USER_LLM_MODEL=mock-chat \
      python -m uvicorn core.main:app --port 8012
    python scripts/mock_llm.py 9099

运行：
    ADMIN_TOKEN=demo-admin python scripts/record_demo.py http://127.0.0.1:8012

输出：docs/media/demo.webm / demo.mp4（需要 ffmpeg）
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8012").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "media"
OUT.mkdir(parents=True, exist_ok=True)
ADMIN = os.environ.get("ADMIN_TOKEN", "demo-admin")

CAPTION_JS = """
(text) => {
  let el = document.getElementById('__demo_caption');
  if (!el) {
    el = document.createElement('div');
    el.id = '__demo_caption';
    el.style.cssText = [
      'position:fixed','left:50%','bottom:26px','transform:translateX(-50%)',
      'z-index:2147483647','background:rgba(14,92,85,.94)','color:#F7F5F0',
      'font:600 15px/1.6 -apple-system,PingFang SC,Microsoft YaHei,sans-serif',
      'padding:10px 22px','border-radius:999px','box-shadow:0 8px 28px rgba(0,0,0,.18)',
      'pointer-events:none','letter-spacing:.2px','max-width:86vw','text-align:center'
    ].join(';');
    document.body.appendChild(el);
  }
  el.textContent = text;
}
"""

TOKEN_JS = "localStorage.setItem('cs-agent-app', JSON.stringify({state:{adminToken:%s},version:0}))" % json.dumps(ADMIN)


def caption(page, text, wait=0):
    try:
        page.evaluate(CAPTION_JS, text)
    # aqg: top-level boundary — 字幕失败不能中断整段录制
    except Exception:
        pass
    if wait:
        page.wait_for_timeout(wait)


def goto(page, path, text, wait=2200):
    page.goto(f"{BASE}{path}", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    caption(page, text, wait)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(ROOT / ".demo" / "video"),
            record_video_size={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.add_init_script(TOKEN_JS)

        # 1. 访客对话
        goto(page, "/", "① 访客入口：Web 与 QQ 共用同一套 Agent 内核")
        page.locator(".wc").first.click()
        page.wait_for_timeout(2200)
        caption(page, "知识库命中：答案带引用与引擎信息，全程可追溯", 2200)
        page.locator(".composer-bar textarea").fill("我的订单什么时候发货")
        page.press(".composer-bar textarea", "Enter")
        page.wait_for_timeout(2600)
        caption(page, "订单类问题自动路由到业务工具，未接入就如实转人工", 2400)

        # 2. 运行总览
        goto(page, "/dashboard", "② 运行总览：会话、渠道、升级原因、工具排行实时可见", 3200)

        # 3. 会话中心
        goto(page, "/sessions", "③ 会话中心：按状态筛选、关键字搜索、服务端分页", 1600)
        page.get_by_text("待接管", exact=True).first.click()
        page.wait_for_timeout(1500)
        caption(page, "待接管队列与升级原因一一对应", 1600)
        page.locator("tbody tr").first.click()
        page.wait_for_timeout(1800)
        caption(page, "点开即可回放完整对话，坐席无需切系统", 2200)

        # 4. 动作审计
        goto(page, "/actions", "④ 动作审计：每个动作都有权限档位、参数与结果", 1600)
        page.get_by_text("校验审计链").click()
        page.wait_for_timeout(1600)
        caption(page, "SHA-256 哈希链一键校验，防篡改可举证", 2200)
        page.locator("tbody tr").first.click()
        page.wait_for_timeout(1400)
        caption(page, "参数与结果全量留痕，敏感字段自动打码", 2000)

        # 5. 任务运行
        goto(page, "/runs", "⑤ 任务运行：多步 ReAct，思考与观察逐步回放", 1500)
        page.get_by_role("button", name="步骤").first.click()
        page.wait_for_timeout(1800)
        caption(page, "每一步思考、动作、观察都可回放，失败也能定位", 2400)

        # 6. 坐席工作台
        goto(page, "/operator", "⑥ 坐席工作台：接管后机器人停嘴，访客消息实时进线", 1600)
        page.get_by_text("接管并对话").first.click()
        page.wait_for_timeout(1600)
        caption(page, "接管即锁定归属，回复通过 WebSocket 实时送达访客", 1600)
        box = page.locator(".op-foot textarea")
        if box.count():
            box.fill("您好，我是坐席小周，退款已为您加急处理。")
            page.get_by_role("button", name="回复").last.click()
            page.wait_for_timeout(1500)
        caption(page, "人工回复与 AI 回答同屏，交接零成本", 2200)

        # 7. 管理中心
        goto(page, "/settings/params", "⑦ 管理中心：模型、检索、护栏、知识库即改即生效", 2200)
        goto(page, "/settings/kb", "知识库支持文档导入与试召回，调参不靠猜", 1500)
        page.locator("input[placeholder*='试召回']").fill("怎么开发票")
        page.get_by_role("button", name="试召回").click()
        page.wait_for_timeout(1800)
        caption(page, "试召回实时显示命中条目与分数", 2200)

        goto(page, "/", "知客 Zhike · 可接渠道、可审计、可部署的客服 Agent", 2200)
        caption(page, "MIT 开源 · GitCode / Gitee / GitHub 三平台同步", 2200)

        page.wait_for_timeout(800)
        ctx.close()
        browser.close()

    videos = sorted((ROOT / ".demo" / "video").glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not videos:
        print("没有找到录制文件")
        return 1
    src = videos[-1]
    webm = OUT / "demo.webm"
    if webm.exists():
        webm.unlink()
    src.replace(webm)
    print("saved", webm, webm.stat().st_size)

    mp4 = OUT / "demo.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(webm),
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("ffmpeg 转码失败：", r.stderr[-500:])
        return 1
    print("saved", mp4, mp4.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
