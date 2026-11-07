"""离线路由收尾只处理原请求；不重新派发、不恢复不明取消责任。"""
from types import SimpleNamespace

from ...common.nofollow_fs import list_names_beneath
from ..daemon_metadata import process_identity_is_live
from ..io import read_json_file_report, validated_gateway_request_fingerprint
from ..standalone_sources import StandaloneSourceUnproven


def candidates(paths):
    """有界于当前 pending/processing 集合，不遍历终态历史。"""
    if not paths.root.exists():
        return []
    result = []
    for directory in (paths.inbox, paths.processing):
        for name in list_names_beneath(paths.root, directory.relative_to(paths.root).parts):
            if not name.endswith('.json'):
                continue
            path = directory / name
            report = read_json_file_report(path)
            if report.load_error is not None:
                raise StandaloneSourceUnproven('migration_request_unreadable')
            row = report.payload
            if row.get('route') is None:
                continue
            validated_gateway_request_fingerprint(row, path.stem)
            if (row.get('conversation_claim') is not None or row.get('cancellation_control')
                    or row.get('cancel_requested') or row.get('active_turn_recovery')):
                raise StandaloneSourceUnproven('migration_request_responsibility_unsettled')
            if directory == paths.processing and process_identity_is_live(row.get('lease_process_identity')) is not False:
                raise StandaloneSourceUnproven('migration_request_executor_unproven')
            result.append((path, row))
    return result


def close_requests(paths, lifecycle, requests):
    """通过原 close-out producer 提交，返回之后才记录该阶段完成。"""
    from ..io import write_json_file_atomic
    from ..multiprocess_unroutable_closeout import close_out_unroutable_processing
    from ..recovery import repair_gateway_terminal_request
    from ..response_renderer import read_gateway_terminal_envelope_report

    lifecycle.require_held()
    context = SimpleNamespace(standalone_lifecycle=lifecycle)
    closed = []
    current = {row['id']: (path, row) for path, row in candidates(paths)}
    if set(current) - {request['id'] for request in requests}:
        raise StandaloneSourceUnproven('migration_request_set_changed')
    for request in requests:
        rid = request['id']
        if rid in current:
            path, row = current[rid]
            if validated_gateway_request_fingerprint(row, rid) != request['fingerprint']:
                raise StandaloneSourceUnproven('migration_request_changed')
            close_out_unroutable_processing(paths, path, row, context, 'stale_generation_route')
        else:
            terminal = read_gateway_terminal_envelope_report(paths.terminal / (rid + '.json'), request_id=rid)
            if (terminal.load_error is not None or not terminal.payload
                    or validated_gateway_request_fingerprint(terminal.payload, rid) != request['fingerprint']):
                raise StandaloneSourceUnproven('migration_request_terminal_missing')
            canonical = terminal.payload
            target = paths.done if canonical.get('status') == 'done' else paths.failed
            lifecycle.require_held()
            repair_gateway_terminal_request(paths, target, rid, canonical['terminal_response'])
            write_json_file_atomic(paths.responses / (rid + '.json'), canonical['terminal_response'])
        closed.append(rid)
    return closed
