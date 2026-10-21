# syntax=docker/dockerfile:1
# ---- 阶段一：构建前端控制台 ----
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 阶段二：Python 运行时（core + gateway 共用镜像）----
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai
COPY requirements.txt .
# 构建机网络抖动常见：加重试与超时，避免一次 DNS 失败就让整次构建失败
RUN pip install --no-cache-dir --retries 8 --timeout 60 -r requirements.txt
COPY core ./core
COPY gateway ./gateway
COPY executor ./executor
COPY scripts ./scripts
COPY --from=ui /ui/dist ./frontend/dist
# 单端口模式：core 直接托管控制台
ENV CS_AGENT_STATIC_DIR=/app/frontend/dist
EXPOSE 8010
CMD ["python", "-m", "uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
