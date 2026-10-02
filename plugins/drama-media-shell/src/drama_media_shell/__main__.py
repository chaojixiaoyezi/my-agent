# LLM: 模块执行只进入 stdio MCP 主循环，不读取命令行任务、不启动 Gateway 或网络客户端。
# 模块用途: 支持 Python 以 -m drama_media_shell 启动插件。

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
