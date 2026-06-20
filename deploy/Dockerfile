# my-agent 企业规模化容器镜像(Tier 5 部署)。多阶段:build 装依赖、runtime 精简。
# 同一镜像跑两种进程:ASGI 入站(默认 CMD)与 worker(清单里覆盖 command)。
#
# 关键(研究核验三家做法后取最优):
#  - tini 作 PID 1:转发信号给整组 + 回收孤儿子进程(my-agent 跑大量工具子进程);
#    claw 让应用直作 PID 1 会漏收僵尸,长期助手/通道运行时 都上了 tini —— 此处对齐更稳的一方。
#  - 非 root 运行(USER appuser):安全基线。
#  - HEALTHCHECK 打 /healthz:容器层存活探测。

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY agent_py_agent ./agent_py_agent
# 装规模 + 密钥两个 extra(PG/ASGI 驱动 + 加密)到独立前缀,便于 runtime 阶段整体拷贝
RUN pip install --no-cache-dir --prefix=/install ".[scale,secrets]"

FROM python:3.12-slim AS runtime
# tini:轻量 init,PID 1 信号转发 + 僵尸回收
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
COPY --from=build /app/agent_py_agent /app/agent_py_agent
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
# 非 root:固定 uid 便于 K8s runAsUser 对齐
RUN useradd -r -u 10001 -m appuser
USER 10001
EXPOSE 8080
# PID 1 = tini;-g 把信号转发给整个进程组(回收工具子进程)
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
# 默认起 ASGI 入站;worker Deployment 用 command 覆盖成 worker_entry
CMD ["python", "-m", "agent_py_agent.agent.asgi_entry"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"]
