# LLM: 原生Unix连接的内核peer观察；仅PID事实，不提供C租约/owner准入，也不接受正文声明。
# 模块用途: 为后续原issuer接线提供可信连接进程观察点。
import socket
import struct
import sys

# LLM: Darwin sys/un.h实际SDK预处理读回SOL_LOCAL=0、LOCAL_PEERPID=0x002；不从配置或请求修改。
# 参数用途: macOS内核Unix peer PID选项，Python未导出相应符号。
# 参数用途: _DARWIN_SOL_LOCAL_PROTOCOL是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_DARWIN_SOL_LOCAL_PROTOCOL = 0
# 参数用途: _DARWIN_LOCAL_PEERPID_PROTOCOL是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_DARWIN_LOCAL_PEERPID_PROTOCOL = 0x002


# LLM: 无真实peer或目标平台能力缺失时失败关闭，不回退环境/JSON/调用方PID。
# 类用途: 表示无法读取该连接的可信内核peer。
class PeerCredentialsUnavailable(RuntimeError):
    error_code = 'WORKER_PEER_UNAVAILABLE'


# LLM: 接受实际plain Unix socket；返回内核PID，出生/原Popen/登记仍由原issuer完整核验。
# 函数用途: 读取真实连接者的PID，不把socket创建者或消息内容当身份。
def kernel_peer_pid(connection):
    if type(connection) is not socket.socket or connection.family != socket.AF_UNIX:
        raise PeerCredentialsUnavailable('original_unix_socket_required')
    if sys.platform == 'darwin':
        level, option, shape = _DARWIN_SOL_LOCAL_PROTOCOL, _DARWIN_LOCAL_PEERPID_PROTOCOL, '=i'
    elif sys.platform == 'linux' and hasattr(socket, 'SO_PEERCRED'):
        level, option, shape = socket.SOL_SOCKET, socket.SO_PEERCRED, '=iII'
    else:
        raise PeerCredentialsUnavailable('kernel_peer_platform_unavailable')
    try:
        if connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise PeerCredentialsUnavailable('unix_stream_socket_required')
        raw = connection.getsockopt(level, option, struct.calcsize(shape))
        pid = struct.unpack(shape, raw)[0]
    except (OSError, struct.error) as error:
        raise PeerCredentialsUnavailable('kernel_peer_unavailable') from error
    if pid <= 0:
        raise PeerCredentialsUnavailable('kernel_peer_unavailable')
    return pid
