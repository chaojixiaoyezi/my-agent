# LLM: 模块入口仅启动 stdio 服务；浏览器子进程的关闭由 server.main 的退出路径负责。
# 模块用途: 支持隔离解释器执行 python -m browser_lite。

from .server import main

raise SystemExit(main())
