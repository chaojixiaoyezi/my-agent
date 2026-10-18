# LLM: 仅用原v4 codec读规范guidance，不运行迁移/投影修复/认领；普通坏结构保领域错误，原typed读取失败原样抛出。
# 模块用途: 让C输入与W控制观察共用一份纯读解码，不把缺失或旧回执猜成已消费。
from ..conversation.store_guidance_records import GuidanceOnceReceipt
from ..runtime_errors import DataCorruptionError
from .io import read_json_file_report


# LLM: 原control调用默认阻塞/旧schema返回None语义不变；C选False只尝试原锁，busy不变不存在，strict旧版保持未知。
# LLM: 可选deadline仅透传同一原JSON读预算；None默认行为不变，缺席/忙/旧schema仍各守原事实，不授队列或append许可。
# 函数用途: 读取规范guidance回执；不写正文、迁移或修索引。
def read_guidance_observation(path, *, blocking=True, strict=False, deadline=None):
    try:
        options = {} if blocking else {'blocking': False}
        if deadline is not None:
            options['deadline'] = deadline
        report = read_json_file_report(path, context='gateway.worker.steer.observe', **options)
    except RecursionError as error:
        from .input_delivery_service import _raise_input_transport_uncertain

        _raise_input_transport_uncertain(error)
        raise DataCorruptionError(f'guidance observation has excessive nesting: {path.name}') from error
    if not blocking and report.lock_busy:
        raise BlockingIOError('guidance observation is busy')
    if report.load_error is not None or (not report.payload and path.exists()):
        raise DataCorruptionError(f'guidance observation is unreadable: {path.name}')
    if not report.payload:
        return None
    if report.payload.get('schema_version') != 'conversation_guidance_once.v4':
        if strict:
            raise DataCorruptionError('guidance observation schema is unsupported')
        return None
    try:
        return GuidanceOnceReceipt.from_dict(report.payload)
    except (TypeError, ValueError, OverflowError) as error:
        from .input_delivery_service import _raise_input_transport_uncertain

        _raise_input_transport_uncertain(error)
        raise DataCorruptionError(f'guidance observation has invalid fields: {path.name}') from error
