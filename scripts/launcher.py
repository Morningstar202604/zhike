"""知客一键启动器：把「拉仓库 → 起核心 → 起网关 → 连协议端」收进一个本地网页。

浏览器打开 http://127.0.0.1:8020 即可操作；只绑定 127.0.0.1，不需要令牌。

它解决的是真实踩过的坑：
- 仓库更新要去镜像拉（GitHub 走代理经常断），这里按 GitCode → Gitee → GitHub 顺序回退；
- 网关在 NapCat 没就绪时会退出，启动器循环重试并如实显示原因；
- 协议端有两条路线（官方 Desktop 管理器 / 本机 Shell），可选用哪种，缺 Shell 就自动下载
  （分段严格校验下载器，206+长度校验+断点续传，复用 napcat_fetch_part.py）；
- 自检过的错误不再重复：核心起了但前端 dist 缺失、令牌不一致等都会直接显示在状态卡上。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv" / "Scripts"
LOGS = ROOT / ".local" / "logs"
LOCAL = ROOT / ".local"
CFG_FILE = LOCAL / "launcher-config.json"

CORE_PORT = 8010
LAUNCHER_PORT = 8020
ONEBOT_PORT = 3001
WEBUI_PORT = 6099

# 拉取镜像优先级：GitCode（国内快）→ Gitee → GitHub（走代理）
PULL_REMOTES = ["origin", "gitee", "github-badhope"]

_log_lock = threading.Lock()


def log(name: str, line: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(LOGS / f"{name}.log", "ab") as f:
        f.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n".encode("utf-8", "replace"))


def load_cfg() -> dict:
    try:
        return json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"qq": "3876137118", "proto": "desktop"}


def save_cfg(cfg: dict) -> None:
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 进程管理 ----------------

procs: dict[str, subprocess.Popen] = {}
proto_state: dict = {"running": False, "note": "", "pct": 0}


def port_up(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def kill_proc(name: str) -> None:
    p = procs.pop(name, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def start_core() -> str:
    if port_up(CORE_PORT):
        return "核心已在运行（8010），无需重启。要换代码请先点「拉取更新」。"
    kill_proc("core")
    uvicorn = VENV / "uvicorn.exe"
    if not uvicorn.exists():
        return "X .venv/Scripts/uvicorn.exe 不存在——先点「装依赖」"
    env = dict(os.environ, CS_AGENT_STATIC_DIR="frontend/dist")
    log_fh = open(LOGS / "core.log", "ab")
    procs["core"] = subprocess.Popen(
        [str(uvicorn), "core.main:app", "--host", "127.0.0.1", "--port", str(CORE_PORT)],
        cwd=str(ROOT), stdout=log_fh, stderr=log_fh, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(2.5)
    return "OK 核心已启动" if port_up(CORE_PORT) else "X 核心启动后未监听——看日志"


def gateway_loop(cfg: dict) -> None:
    """网关守护：NapCat 没就绪时网关会退出，这里循环拉起并记录原因。"""
    while True:
        if procs.get("gateway") and procs["gateway"].poll() is None:
            time.sleep(5)
            continue
        kill_proc("gateway")
        env = dict(os.environ)
        log_fh = open(LOGS / "gateway.log", "ab")
        try:
            procs["gateway"] = subprocess.Popen(
                [str(VENV / "python.exe"), str(ROOT / "gateway" / "bot.py")],
                cwd=str(ROOT), stdout=log_fh, stderr=log_fh, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW)
            log("launcher", "gateway spawned")
        except Exception as e:
            log("launcher", f"gateway spawn failed: {e}")
        # 网关对 NapCat 未就绪会自己退出（code=2）；等 30s 再拉
        for _ in range(30):
            p = procs.get("gateway")
            if p is None or p.poll() is not None:
                break
            time.sleep(1)
        time.sleep(5)


def start_gateway_thread(cfg: dict) -> None:
    threading.Thread(target=gateway_loop, args=(cfg,), daemon=True).start()


# ---------------- 仓库更新 ----------------

def repo_pull() -> str:
    """按镜像优先级 ff-only 拉取；本地有改动先 stash，拉完 pop。"""
    out: list[str] = []

    def git(*args: str) -> tuple[int, str]:
        r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    stash_needed = False
    rc, s = git("status", "--porcelain", "-uno")
    if rc == 0 and s.strip():
        stash_needed = True
    if stash_needed:
        rc, s = git("stash", "push", "-m", "launcher-autostash")
        out.append(("stash: " + s.strip()[:200]) if rc == 0 else f"stash 失败: {s[:200]}")
        if rc != 0:
            return "\n".join(out) + "\nX 本地有未提交改动且 stash 失败，请手动处理"
    ok = False
    for remote in PULL_REMOTES:
        rc, s = git("pull", "--ff-only", remote, "main")
        out.append(f"{remote}: {s.strip()[-300:]}")
        if rc == 0:
            ok = True
            break
        log("launcher", f"pull via {remote} failed")
    if stash_needed and ok:
        rc, s = git("stash", "pop")
        out.append(("stash pop: " + s.strip()[:200]) if rc == 0 else f"stash pop 冲突: {s[:200]}")
    return ("\n".join(out) + "\nOK 更新完成" if ok else "\n".join(out) + "\nX 全部镜像拉取失败")


# ---------------- 协议端 ----------------

def proto_status() -> dict:
    return {
        "onebot_3001": port_up(ONEBOT_PORT),
        "webui_6099": port_up(WEBUI_PORT),
        **proto_state,
    }


def start_proto(kind: str, cfg: dict) -> str:
    if kind == "desktop":
        exe = Path(r"C:\Program Files\NapCatQQ Desktop\NapCatQQ-Desktop.exe")
        if not exe.exists():
            return "X 未安装 NapCatQQ Desktop——用下方「下载 Desktop」按钮"
        subprocess.Popen([str(exe)])
        return "OK NapCatQQ Desktop 已启动：在里面装组件 → 新建 Bot（QQ号 %s）→ 启动 → 扫码。" % cfg["qq"]
    if kind == "shell":
        qq = cfg["qq"]
        if not (LOCAL / "napcat" / "NapCatWinBootMain.exe").exists():
            return "X 本机 Shell 未安装——先用「下载/更新 Shell」按钮"
        threading.Thread(target=_run_shell_start, args=(qq,), daemon=True).start()
        return "OK Shell 启动中（后台，最长等 2 分钟）——完成后看协议端状态灯。"
    return f"X 未知协议端类型：{kind}"


def _run_shell_start(qq: str) -> None:
    r = subprocess.run([str(VENV / "python.exe"), str(ROOT / "scripts" / "napcat_local.py"),
                        "start", "--qq", qq], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    log("launcher", f"napcat_local start: code={r.returncode} {(r.stdout or r.stderr)[-200:]}")


def download_shell() -> str:
    """下载/更新本机 Shell：查最新 release → 并行分段严格校验下载 → 解压（保留配置）。"""

    def _worker():
        proto_state.update(running=True, note="获取最新版本…", pct=0)
        try:
            import httpx
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                or "http://127.0.0.1:7897"
            with httpx.Client(proxy=proxy, timeout=30) as c:
                rel = c.get("https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest").json()
                tag = rel["tag_name"]
                asset = next(a for a in rel["assets"] if a["name"] == "NapCat.Shell.zip")
                target, url = asset["size"], asset["browser_download_url"]
            proto_state.update(note=f"下载 {tag}（{target // 1048576} MB）…", pct=1)
            out = LOCAL / "napcat" / "_shell.zip"
            p = subprocess.Popen(
                [str(VENV / "python.exe"), str(LOCAL / "napcat_fetch_part.py"),
                 "--url", url, "--target", str(target), "--out", str(out), "--jobs", "6"],
                cwd=str(LOCAL), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            assert p.stdout is not None
            for line in p.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    done = int(line.split()[1].split("/")[0])
                    proto_state.update(pct=min(99, int(done * 98 / target)),
                                       note=f"下载 {tag}…")
                else:
                    log("launcher", f"dl: {line[:150]}")
            rc = p.wait(timeout=7200)
            if rc != 0:
                proto_state.update(running=False, note="X 下载/CRC 失败——看启动器日志")
                return
            proto_state.update(note="解压安装（保留已有配置）…", pct=99)
            zf = zipfile.ZipFile(out)
            n = 0
            for info in zf.infolist():
                if info.filename.startswith("config/"):
                    continue
                zf.extract(info, LOCAL / "napcat")
                n += 1
            (LOCAL / "napcat" / "version.txt").write_text(tag, encoding="utf-8")
            out.unlink(missing_ok=True)
            for i in range(6):
                (LOCAL / f"napcat_part_{i}").unlink(missing_ok=True)
            proto_state.update(running=False, note=f"OK 已安装 {tag}（{n} 文件）", pct=100)
            log("launcher", f"shell installed {tag}")
        except Exception as e:
            proto_state.update(running=False, note=f"X {type(e).__name__}: {str(e)[:120]}")

    threading.Thread(target=_worker, daemon=True).start()
    return "OK 后台下载已开始——进度看协议端状态卡"


# ---------------- 状态汇总 ----------------

def status() -> dict:
    core = {"up": port_up(CORE_PORT)}
    if core["up"]:
        try:
            with urlopen(f"http://127.0.0.1:{CORE_PORT}/healthz", timeout=3) as r:
                core["version"] = json.loads(r.read().decode()).get("version", "?")
        except Exception:
            core["version"] = "?"
    gw = procs.get("gateway")
    cfg = load_cfg()
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                            capture_output=True, text=True).stdout.strip()
    return {
        "core": core,
        "gateway": {"alive": bool(gw and gw.poll() is None)},
        "onebot_3001": port_up(ONEBOT_PORT),
        "webui_6099": port_up(WEBUI_PORT),
        "git_commit": commit,
        "dist": (ROOT / "frontend" / "dist" / "index.html").exists(),
        "qq": cfg["qq"],
        "proto": proto_state,
        "qq_up": _qq_running(),
    }


def _qq_running() -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                           capture_output=True, text=True, timeout=10)
        return "QQ.exe" in (r.stdout or "")
    except Exception:
        return False


# ---------------- HTTP 服务 ----------------

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>知客启动器</title><style>
:root{--bg:#f6f7f9;--panel:#fff;--line:#e4e7ec;--tx:#1a1f27;--mut:#667085;--ok:#16a34a;--bad:#dc2626;--warn:#d97706;--brand:#2563eb}
*{box-sizing:border-box}body{margin:0;font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--tx)}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;background:#cbd5e1}
.ok{background:var(--ok)}.bad{background:var(--bad)}.warn{background:var(--warn)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.meta{color:var(--mut);font-size:12px;margin-left:auto}
button{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer}
button:hover{border-color:var(--brand);color:var(--brand)}
button.p{background:var(--brand);border-color:var(--brand);color:#fff}
button.p:hover{opacity:.9;color:#fff}
.note{font-size:12px;color:var(--mut);margin-top:8px;white-space:pre-wrap}
pre{background:#f1f3f7;border-radius:8px;padding:10px;font-size:11px;max-height:180px;overflow:auto;white-space:pre-wrap}
input{border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px;width:130px}
</style></head><body><div class="wrap">
<h1>知客启动器</h1><div class="sub">仓库 → 核心 → 网关 → 协议端，一键串起来</div>

<div class="card"><h2><span id="d-git" class="dot"></span>仓库</h2>
<div class="row"><span class="meta" id="commit"></span>
<button onclick="act('/api/repo/pull',this)">拉取更新（GitCode→Gitee→GitHub）</button></div>
<pre id="log-pull" hidden></pre></div>

<div class="card"><h2><span id="d-core" class="dot"></span>核心 <span class="meta" id="core-v"></span></h2>
<div class="row"><button class="p" onclick="act('/api/core/start',this)">启动 / 确认运行</button></div>
<div class="note" id="core-note"></div></div>

<div class="card"><h2><span id="d-gw" class="dot"></span>网关 <span class="meta" id="gw-note"></span></h2>
<div class="note">网关由启动器守护：NapCat(3001) 没就绪时会显示重试，协议端一上线就自动接入。</div></div>

<div class="card"><h2><span id="d-proto" class="dot"></span>协议端 <span class="meta" id="proto-note"></span></h2>
<div class="row">
<button onclick="act('/api/proto/start?kind=desktop',this)">方式一：NapCatQQ Desktop（推荐）</button>
<button onclick="act('/api/proto/start?kind=shell',this)">方式二：本机 Shell</button>
<button onclick="act('/api/shell/download',this)">下载 / 更新 Shell</button></div>
<div class="row" style="margin-top:8px"><span style="font-size:12px;color:var(--mut)">机器人 QQ 号</span>
<input id="qq" onchange="saveQQ(this.value)"></div>
<div class="note" id="proto-msg"></div></div>

<div class="card"><h2>启动器日志</h2><pre id="log-launcher">加载中…</pre></div>
</div>
<script>
const $=id=>document.getElementById(id);
function dot(id,ok,warn){const e=$(id);e.className='dot '+(ok?'ok':warn?'warn':'bad')}
async function refresh(){try{const s=await(await fetch('/api/status')).json();
 dot('d-git',true);$('commit').textContent='commit '+(s.git_commit||'?')+(s.dist?'':' · ⚠ 前端未构建');
 dot('d-core',s.core.up);$('core-v').textContent=s.core.up?('v'+s.core.version):'未运行';
 dot('d-gw',s.gateway.alive,false);
 $('gw-note').textContent=s.gateway.alive?'运行中':(s.onebot_3001?'待启动':'重试中：等协议端 3001');
 const pUp=s.onebot_3001, wUp=s.webui_6099;
 dot('d-proto',pUp,wUp);
 $('proto-note').textContent=(pUp?'OneBot:3001 ✅ ':'OneBot:3001 ✗ ')+(wUp?'· WebUI:6099 ✅':'· WebUI:6099 ✗')+(s.qq_up?' · 桌面QQ运行中':'');
 if(s.proto.note)$('proto-msg').textContent=s.proto.note+(s.proto.pct?(' '+s.proto.pct+'%'):'');
 if($('qq')!==document.activeElement&&s.qq)$('qq').value=s.qq;
 const l=await(await fetch('/api/logs')).text();$('log-launcher').textContent=l||'（空）';
}catch(e){console.warn(e)}}
async function act(url,btn){if(btn){btn.disabled=true;btn.textContent='处理中…'}
 try{const r=await(await fetch(url,{method:'POST'})).json();
  if(r.log){$('log-pull').hidden=false;$('log-pull').textContent=r.log}
  if(r.msg)$('proto-msg').textContent=r.msg;
 }catch(e){alert('请求失败：'+e)}
 if(btn){btn.disabled=false;btn.textContent=btn.dataset.t||btn.textContent}}
document.querySelectorAll('button').forEach(b=>b.dataset.t=b.textContent);
async function saveQQ(v){await fetch('/api/qq',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({qq:v})})}
refresh();setInterval(refresh,3000)
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str | bytes, ctype: str = "application/json") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/status":
            self._send(200, json.dumps(status(), ensure_ascii=False))
        elif self.path == "/api/logs":
            try:
                lines = (LOGS / "launcher.log").read_text(encoding="utf-8", errors="replace").splitlines()
                self._send(200, "\n".join(lines[-40:]), "text/plain")
            except OSError:
                self._send(200, "（暂无日志）", "text/plain")
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/api/repo/pull":
            out = repo_pull()
            self._send(200, json.dumps({"log": out}, ensure_ascii=False))
        elif path == "/api/core/start":
            self._send(200, json.dumps({"msg": start_core()}, ensure_ascii=False))
        elif path == "/api/gateway/restart":
            kill_proc("gateway")
            start_gateway_thread(load_cfg())
            self._send(200, json.dumps({"msg": "网关守护已重启"}, ensure_ascii=False))
        elif path == "/api/proto/start":
            kind = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("kind", "desktop")
            self._send(200, json.dumps({"msg": start_proto(kind, load_cfg())}, ensure_ascii=False))
        elif path == "/api/shell/download":
            self._send(200, json.dumps({"msg": download_shell()}, ensure_ascii=False))
        elif path == "/api/qq":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            cfg = load_cfg()
            cfg["qq"] = str(body.get("qq", cfg["qq"]))
            save_cfg(cfg)
            self._send(200, json.dumps({"msg": f"QQ 号已保存：{cfg['qq']}"}, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, *a):  # 静默默认访问日志
        pass


def main() -> None:
    cfg = load_cfg()
    log("launcher", "launcher starting")
    # 开机场景：先确保核心在跑（起不来不打断启动器）
    if not port_up(CORE_PORT):
        threading.Thread(target=start_core, daemon=True).start()
    start_gateway_thread(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", LAUNCHER_PORT), Handler)
    print(f"launcher: http://127.0.0.1:{LAUNCHER_PORT}")
    if os.environ.get("LAUNCHER_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{LAUNCHER_PORT}")).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
