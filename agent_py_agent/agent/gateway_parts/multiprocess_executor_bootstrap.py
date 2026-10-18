# LLM: 本文件仅由登记设施 -I 隔离加载；固定随包根路径不使用命令 PYTHONPATH 或工作目录。
# 模块用途: 从私有 FD 恢复可信后台宿主归属，不提供任意模块执行器；保持原FD/身份校验和失败退出125，同步核对委托调用方。
import sys
from pathlib import Path


# LLM: 固定模块先核对完整登记和自身身份，错误退出125且不打印交接正文。
# 函数用途: 调用内部受信委托入口。
def main() -> int:
    if len(sys.argv) != 2:
        return 125
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_delegation import run_delegate

    try:
        return run_delegate(int(sys.argv[1]))
    except Exception:
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
