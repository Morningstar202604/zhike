#!/usr/bin/env bash
# 知客 Zhike 服务器一键部署/升级脚本（在部署机上执行）
# 用法：
#   bash <(curl -fsSL <raw-url>/scripts/deploy.sh)          # 首次部署（两阶段：先生成 .env 再启动）
#   APP_DIR=/opt/zhike bash scripts/deploy.sh               # 指定安装目录
# 升级：重跑本脚本即可（git pull + 重建镜像 + 滚动更新）
set -euo pipefail

REPO_URL="${REPO_URL:-https://gitcode.com/badhope/zhike.git}"
APP_DIR="${APP_DIR:-/opt/zhike}"

command -v docker >/dev/null || { echo "❌ 未检测到 docker，请先安装：curl -fsSL https://get.docker.com | sh"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "❌ 缺少 docker compose 插件"; exit 1; }

# ---- 拉取代码 ----
if [ ! -d "$APP_DIR/.git" ]; then
  echo "▸ 克隆仓库到 $APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
else
  echo "▸ 更新代码（$APP_DIR）"
  git -C "$APP_DIR" pull --ff-only
fi
cd "$APP_DIR"

# ---- 阶段一：生成 .env（含强随机令牌）----
if [ ! -f .env ]; then
  cp deploy/.env.production.example .env
  sed -i "s/changeme-admin-token/$(openssl rand -hex 16)/" .env
  sed -i "s/changeme-channel-token/$(openssl rand -hex 16)/" .env
  # 执行器签名令牌：core 与 executor 必须一致；不生成则容器内执行器无鉴权
  printf '\nEXECUTOR_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
  echo
  echo "✅ 已生成 .env（两把令牌已自动随机化，请记录）："
  grep -E "^(ADMIN_TOKEN|CHANNEL_TOKEN)=" .env
  echo
  echo "▸ 下一步（手动）：" 
  echo "  1. 编辑 $APP_DIR/.env 填 NAPCAT_QQ（机器人 QQ 号）"
  echo "  2. 按需填 USER_LLM_API_KEY（不填=规则模式）"
  echo "  3. 重跑本脚本启动服务"
  exit 0
fi

# 老 .env 升级：没有执行器令牌就补一个，否则执行器会拒绝在 0.0.0.0 上启动
if ! grep -q "^EXECUTOR_TOKEN=" .env; then
  printf '\nEXECUTOR_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
  echo "▸ 已为老 .env 补生成 EXECUTOR_TOKEN"
fi

# 执行器工作目录与策略：容器内 allow_roots 必须是 /work（宿主路径在容器里不存在）。
# compose 文件在 deploy/ 下，相对路径按该目录解析，默认落 deploy/exec_work。
EXECUTOR_DIR="${EXECUTOR_WORKDIR:-$APP_DIR/deploy/exec_work}"
mkdir -p "$EXECUTOR_DIR"
if [ ! -f "$EXECUTOR_DIR/policy.json" ]; then
  python3 - "$EXECUTOR_DIR/policy.json" <<'PYEOF'
import json, pathlib, sys
src = pathlib.Path("executor/policy.example.json")
dst = pathlib.Path(sys.argv[1])
data = json.loads(src.read_text(encoding="utf-8"))
data["fs"]["allow_roots"] = ["/work"]
dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print("已生成执行器策略:", dst, "allow_roots=/work")
PYEOF
fi

if ! grep -q "^NAPCAT_QQ=[0-9]" .env; then
  echo "⚠️  .env 里还没填 NAPCAT_QQ（机器人 QQ 号），填好后重跑本脚本"
  exit 1
fi

# ---- 阶段二：构建并启动 ----
# 注意：必须在仓库根执行，compose 里的构建上下文是 ..（仓库根），env_file 也是 ../.env
echo "▸ 构建镜像并启动（首次需数分钟）"
docker compose -f deploy/docker-compose.yml up -d --build

echo "▸ 等待 core 健康检查…"
for i in $(seq 1 30); do
  if curl -fsS -m 3 http://127.0.0.1:8010/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done

docker compose -f deploy/docker-compose.yml ps
echo
if curl -fsS -m 3 http://127.0.0.1:8010/healthz >/dev/null 2>&1; then
  echo "✅ core 已就绪：http://$(curl -fsS -m 3 ifconfig.me 2>/dev/null || echo '<服务器IP>'):8010"
  grep -E "^ADMIN_TOKEN=" .env | sed 's/^/   控制台令牌：/'
  echo "   QQ 登录：SSH 隧道后浏览器打开 http://127.0.0.1:6099（WebUI 令牌见 docker logs zhike-napcat）"
else
  echo "❌ core 未就绪，查日志：docker logs zhike-core"
  exit 1
fi
