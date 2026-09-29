"""控制台布局审计：双视口逐页检测横向溢出 / 底栏遮挡 / 点击遮挡 / 滚动异常。"""
import io
import json
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010").rstrip("/")
PAGES = ["/dashboard", "/sessions", "/operator", "/logs", "/settings", "/"]
VIEWPORTS = [("desktop", 1440, 900), ("phone", 390, 844)]

JS = """
() => {
  const doc = document.documentElement;
  const issues = [];
  if (doc.scrollWidth > doc.clientWidth + 1) {
    issues.push(`横向溢出: scrollWidth=${doc.scrollWidth} > clientWidth=${doc.clientWidth}`);
  }
  const nav = document.querySelector('nav.sticky, nav[class*="bottom-0"]');
  const navTop = nav ? nav.getBoundingClientRect().top : null;
  const scrollableClip = (el) => {
    let n = el.parentElement;
    while (n && n !== document.body) {
      const st = getComputedStyle(n);
      if (/(auto|scroll)/.test(st.overflowY + st.overflowX)) return true;
      n = n.parentElement;
    }
    return false;
  };
  const clickable = [...document.querySelectorAll('button, a, input, textarea, select')];
  let occluded = 0;
  for (const el of clickable) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cy > window.innerHeight || cx > window.innerWidth) continue;
    const top = document.elementFromPoint(cx, cy);
    if (top && !el.contains(top) && !top.contains(el)) {
      if (scrollableClip(el)) continue;
      const tag = (el.textContent || '').trim().slice(0, 14) || el.getAttribute('aria-label') || el.className.slice(0, 20);
      const chain = [];
      let nn = el.parentElement;
      while (nn && nn !== document.body && chain.length < 5) {
        const st = getComputedStyle(nn);
        chain.push(`${nn.className.toString().slice(0, 24)}[${st.overflowY}/${st.overflowX}]`);
        nn = nn.parentElement;
      }
      issues.push(`点击被遮挡: [${tag}] 被 ${top.tagName}.${(top.className||'').toString().slice(0,26)} 覆盖 | 链: ${chain.join(' > ')}`);
      occluded++;
      if (occluded >= 3) break;
    }
  }
  const pageType = document.querySelector('.chat-app, .op-app, .set-app') ? 'app' : 'scroll';
  if (pageType === 'app') {
    const h = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
    if (h > window.innerHeight + 2) {
      issues.push(`App页文档被撑高: ${h} > ${window.innerHeight}（应内部滚动）`);
    }
  }
  const composer = document.querySelector('.op-foot, .stage-foot');
  if (composer) {
    const r = composer.getBoundingClientRect();
    const vh = window.innerHeight;
    if (r.bottom > vh + 1) issues.push(`输入区超出视口: bottom=${Math.round(r.bottom)} > ${vh}`);
    if (r.height < 40) issues.push(`输入区高度异常: ${Math.round(r.height)}`);
  }
  return issues;
}
"""

report = {}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for label, w, h in VIEWPORTS:
        pg = browser.new_page(viewport={"width": w, "height": h})
        for path in PAGES:
            pg.goto(f"{BASE}{path}", wait_until="domcontentloaded")
            pg.wait_for_timeout(1600)
            issues = pg.evaluate(JS)
            key = f"{label} {path}"
            report[key] = issues
            print(("FAIL " if issues else "PASS ") + key, ("| " + " ; ".join(issues[:2])) if issues else "")
            pg.close()
            pg = browser.new_page(viewport={"width": w, "height": h})
        pg.close()
    browser.close

io.open("layout_audit.json", "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=1))
print("AUDIT_DONE")
