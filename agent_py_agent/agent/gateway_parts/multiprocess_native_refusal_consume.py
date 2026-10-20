# LLM: 仅原execute_worker_claim的materialize词法catch消费，ACK/token不授权限；同canonical及原fullclaim才结束Future，本模块不写账/结输入/P6。
# 模块用途: 核收原C已经提交的本请求失败，让原_reap按True正常收槽；wire-only/错来源仍保原异常。
import os

from .multiprocess_native_commit_client import NativeCommitClient, NativeCommitNotStarted


# LLM: token是调用栈归属而非业务许可；客户端原父出生、creator、原异常和同次seq都独立核，不借finish或之后异常清槽。
# 函数用途: 校验一次本地materialize拒绝确来自刚发生的原RPC。
def require_materialize_rejection(client, error, token, claim):
    source = getattr(error, "_materialize_origin", None)
    if (
        type(client) is not NativeCommitClient
        or type(error) is not NativeCommitNotStarted
        or error._origin is not error
        or error._creator != os.getpid()
        or error._client is not client
        or type(error.seq) is not int
        or error.seq != client._seq
        or type(source) is not tuple
        or len(source) != 3
        or source[0] is not token
        or source[1:] != (claim.request_id, claim.execution_attempt_id)
    ):
        return False
    client._require_original()
    return True


# LLM: 原W/P4/claim来源先有效，再核原canonical/route/fingerprint及P3；occupied只保原释放欠账，不在W释放，不签输入结算完成。
# 函数用途: 在无Agent的首次拒绝处读回本请求终态，缺canonical保持原wire-only异常路径。
def consume_first_refusal(binding, original, client, error, token):
    from ..common.nofollow_fs import read_bytes_beneath
    from . import multiprocess_admission as ma
    from .multiprocess_bootstrap import require_worker_bootstrap
    from .multiprocess_dispatch import require_claimable
    from .multiprocess_dispatch_binding import _require_claim_identity
    from .multiprocess_native_refusal_source import read_claim_payload
    from .multiprocess_native_refusal_terminal import require_refusal_terminal
    from .multiprocess_records import deserialize_record, release_admission
    from .paths import gateway_paths_from_root

    claim, snapshot = binding.claim, binding.snapshot
    if not require_materialize_rejection(client, error, token, claim):
        return False
    require_worker_bootstrap(snapshot.bootstrap, snapshot.bootstrap.launch)
    paths = gateway_paths_from_root(snapshot.bootstrap.launch.gateway_root)
    canonical_path = paths.terminal / (claim.request_id + ".json")
    if (
        read_bytes_beneath(
            paths.root,
            canonical_path.relative_to(paths.root).parts,
            max_bytes=16 * 1024 * 1024,
            require_dir_fd=True,
        )
        is None
    ):
        return False
    canonical = read_claim_payload(paths, canonical_path)
    _require_claim_identity(snapshot, claim, canonical)
    require_refusal_terminal(original, canonical)
    reservation = deserialize_record(snapshot.reservation_bytes)
    require_claimable(snapshot.bootstrap, canonical, reservation)
    expected = release_admission(reservation, canonical)
    record_path = ma.reservation_file(paths, claim.request_id)
    actual = deserialize_record(
        read_bytes_beneath(
            paths.root,
            record_path.relative_to(paths.root).parts,
            max_bytes=16384,
            require_dir_fd=True,
        )
    )
    return actual in (reservation, expected)
