# LLM: 固定C物理初始化RPC；正文仅定位原请求/attempt，不提供owner、路径、代码或写许可，原P3/T/lease仍唯一权威。
# 模块用途: 在原C短门内物化原请求用户目录及种子，让W不再负责这一首写。
import re
from pathlib import Path

from ..user_space.owner_resolver import OwnerIdentity
from .io import read_json_file_report
from .multiprocess_route import validate_worker_route
from .multiprocess_worker_commit import WorkerCommitRejected
from .request_binding import GatewayActiveTurnTransition


# LLM: 只接受有限定位字段，路径拼接前拒穿越/空/特殊类型；字段合法不代表请求/owner许可。
# 函数用途: 验证固定RPC消息形状及单调序号。
def require_materialize_request(request, previous):
    identifier, attempt, seq = request.get('request_id'), request.get('execution_attempt_id'), request.get('seq')
    if (set(request) != {'op', 'seq', 'request_id', 'execution_attempt_id'}
            or type(seq) is not int or seq <= previous
            or type(identifier) is not str or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,255}', identifier)
            or type(attempt) is not str or not attempt or len(attempt) > 512):
        raise WorkerCommitRejected('commit_request_invalid')


# LLM: 由原session真实receive核sender之后调用；owner来自当前canonical请求并由原T/P3复核，不采信RPC正文。
# 函数用途: 注册一次固定C初始化操作，精确原请求/attempt/phase不能重放。
def register_owner_materialization(session, request):
    session._require_original()
    issuer = session._sources[0]
    path = issuer.control.paths.processing / (request['request_id'] + '.json')
    transition = GatewayActiveTurnTransition(path, request['request_id'], request['execution_attempt_id'])

    # LLM: 原T内只读当前route，之后NativeShortOperation同一T再次核原P3和attempt；此读回不是写许可。
    # 函数用途: 从canonical owner三元组选择可信C初始化对象。
    def select_owner():
        payload = read_json_file_report(path, context='native.materialize.owner').payload
        checked = validate_worker_route(payload, issuer._source_origin[0].partition)
        if not checked['ok']:
            raise WorkerCommitRejected('original_native_admission_required')
        return OwnerIdentity(*checked['owner'])

    owner = transition('owner_materialize', select_owner)

    # LLM: 原controller短门同步调用；所有目录/种子物理写都在原C，返回值仅报告完成，不授权后续W写。
    # 函数用途: 按原布局初始化当前用户，不补建共享资源或管理员。
    def materialize():
        return materialize_coordinator_owner_home(issuer._source_origin[0].owner_root, owner)

    return session.register_short_operation(owner, transition, 'owner_materialize', materialize)


# LLM: 仅原C的native短操作或控制首次发表门同步调用；原全局拒门先挡W/受限宿主，调用方仍须核原source/owner/P2/lease，拒门不授写权。
# 函数用途: 复用唯一规范owner布局及nofollow前缀核验创建目录和种子，不构造Agent、模型或管理员。
def materialize_coordinator_owner_home(root, owner):
    from ..common.global_write_guard import require_global_writer
    require_global_writer('owner_materialization')
    from ..user_space.owner_resolver import (
        _owner_directories,
        _owner_home_dir,
        _owner_home_result,
        ensure_owner_home,
    )
    from ..user_space.worker_home import _require_existing_owner_path

    projected = _owner_home_result(root, owner, _owner_home_dir(root, owner))
    targets = {value for value in vars(projected).values() if isinstance(value, Path)}
    targets.update(_owner_directories(projected))
    for target in targets:
        _require_existing_owner_path(root, target)
    actual = ensure_owner_home(root, owner)
    if actual != projected:
        raise WorkerCommitRejected('original_native_owner_home_required')
    return {'owner': [owner.provider, owner.owner_kind, owner.owner_id]}
