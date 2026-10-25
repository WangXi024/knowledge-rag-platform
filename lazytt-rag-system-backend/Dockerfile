# 第一阶段只取得固定版本 uv 二进制，避免在最终镜像内安装额外构建工具。
FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

# Gateway、Worker、Scheduler 共用同一精简 Python 基础镜像，通过 Compose 覆盖启动命令。
FROM python:3.12.10-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

# 固定非 root UID/GID，便于挂载卷预先设置权限，也降低容器逃逸后的权限。
# Worker 使用发行版 FFmpeg/ffprobe 做确定性媒体分段和抽帧；模型调用仍走配置 API。
RUN groupadd --system --gid 10001 rag \
    && useradd --system --uid 10001 --gid rag --home-dir /app --shell /usr/sbin/nologin rag \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
# 先复制依赖清单形成稳定缓存层；只有依赖变化时才重新下载生产包。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 再复制变化频繁的源码、迁移和 Profile 种子，并安装当前项目本身。
ARG BUILD_COMMIT=unknown
LABEL org.opencontainers.image.revision=${BUILD_COMMIT}
COPY src ./src
COPY migrations ./migrations
# 显式管理任务复用既有 API 适配器，不把本地部署脚本或配置秘密放入镜像。
COPY tools/ragflow_account_bootstrap.py tools/ragflow_cloud_model_bootstrap.py tools/ecs_ragflow_bootstrap.py ./tools/
COPY rag-development-spec/configs ./configs
COPY rag-development-spec/configs/application.example.yaml ./configs/application.yaml
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /var/lib/rag/uploads /var/lib/rag/scheduler \
    && chown -R rag:rag /app /var/lib/rag

# 从此处开始及运行期均使用非 root 用户；上传目录已在上一层完成授权。
USER rag

CMD ["uvicorn", "rag_service.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
