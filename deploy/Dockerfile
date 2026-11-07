# my-agent 企业规模化容器镜像(Tier 5 部署)。多阶段:build 装依赖、runtime 精简。
# 同一镜像跑两种进程:ASGI 入站(默认 CMD)与 worker(清单里覆盖 command)。
#
# 运行边界:
#  - tini 作 PID 1:转发信号给整组 + 回收孤儿子进程(my-agent 跑大量工具子进程);
#  - 非 root 运行(USER appuser):避免默认扩大应用权限。
#  - HEALTHCHECK 打 /healthz:容器层存活探测。

# S2-W5B: 短CPU探针未达到4线程3倍门槛，不能声称默认切换已验收。
# 保持py312=1；复核通过后可用 --build-arg py312=0 选择3.14t。
# 普通3.14与3.14t均使用纯Python psycopg，目标系统必须保留libpq5。
ARG py312=1
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM debian:bookworm-slim AS python0
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PYTHON_BIN_DIR=/usr/local/bin
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && uv python install --default 3.14t

FROM python:3.12-slim AS python1
COPY --from=uv /uv /usr/local/bin/uv

FROM python${py312} AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY agent_py_agent ./agent_py_agent
# 独立venv使uv管理的3.14t与/usr/local内的3.12具有一致的包搜索路径。
RUN uv venv --python python /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache ".[scale,secrets]"

FROM python${py312} AS runtime
# tini:轻量 init,PID 1 信号转发 + 僵尸回收。
# bubblewrap:执行节点的强制用户命令隔离；系统包优先于 wheel 内 vendor 离线版本，
# 避免 vendor ELF 与镜像 glibc/libcap 漂移。
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap tini libpq5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/agent_py_agent /app/agent_py_agent
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080 PATH="/opt/venv/bin:${PATH}"
# 非 root:固定 uid 便于 K8s runAsUser 对齐
RUN useradd -r -u 10001 -m appuser
USER 10001
# 构建期验证最终镜像里的 bwrap 能加载；namespace/mount 真自检在 worker 启动和
# K8s readiness 中运行，因为那才是最终节点的真实 seccomp/userns 环境。
RUN python -m agent_py_agent.agent.tooling.sandbox --binary-only --quiet
EXPOSE 8080
# PID 1 = tini;-g 把信号转发给整个进程组(回收工具子进程)
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
# 默认起 ASGI 入站;worker Deployment 用 command 覆盖成 worker_entry
CMD ["python", "-m", "agent_py_agent.agent.asgi_entry"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"]
