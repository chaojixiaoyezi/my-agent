# LLM: 控制RPC只接无FD帧；内核FD领取与拒绝清理必须同一路，结果不授业务许可。
# 模块用途: 为C责任消息和W回复共用严格无资源控制信封，不能用普通receive漏掉复杂消息资源。
from .multiprocess_kernel_message import KernelMessageReceiver
from .multiprocess_kernel_message_fds import close_fds
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: 先完成同次native receive并保留真实sender；多余FD副本仅清理，不解释正文、更新序号或登记责任。
# 函数用途: 读取原无FD控制帧，拒绝意外资源且不关闭发送者原FD。
def receive_control_packet(receiver, *, timeout):
    payload, peer_pid, descriptors = KernelMessageReceiver.receive_fds(receiver, timeout=timeout)
    if descriptors:
        close_fds(descriptors)
        raise WorkerCommitRejected('native_control_fds_unexpected')
    return payload, peer_pid


# LLM: 与原无FD控制路径相同kernel receive/资源清理，只附同次不可变AUDIT instance；无此平台证据返回None由上层typed failclosed，不读last-peer或再recv。
# 函数用途: 在READY活实例核验前取得该消息真实内核代次，原二元调用者继续原接口。
def receive_control_packet_with_instance(receiver, *, timeout):
    packet = KernelMessageReceiver.receive_fds(receiver, timeout=timeout)
    payload, peer_pid, descriptors = packet
    if descriptors:
        close_fds(descriptors)
        raise WorkerCommitRejected('native_control_fds_unexpected')
    return payload, peer_pid, getattr(packet, 'sender_instance', None)
