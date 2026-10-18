# LLM: 仅原C选择这个固定-I入口，不接受任意模块或运输source对象；保持原FD、身份校验和失败退出125合同，同步核对native host调用方。
# 模块用途: 通过原固定native host入口关闭匿名交接FD、认证真实宿主，再进入原后台业务。
import sys
from pathlib import Path


# LLM: 保持原五字段v1帧与上限，只加载同包固定模块；失败退出125且不打印正文，不能增加授权或备用加载入口。
# 函数用途: 从可信包根加载唯一native host委托入口，参数不完整时直接退出。
def main():
    if len(sys.argv) != 2:
        return 125
    sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
    from agent_py_agent.agent.gateway_parts.multiprocess_native_host import run_native_delegate
    try:
        return run_native_delegate(int(sys.argv[1]))
    except Exception:
        return 125


if __name__ == '__main__':
    raise SystemExit(main())
