# LLM: 模块入口仅启动 stdio 服务，进程生命周期与撤销由宿主原链路负责。
# 模块用途: 支持隔离解释器执行 python -m harness_console。

from .server import main

raise SystemExit(main())
